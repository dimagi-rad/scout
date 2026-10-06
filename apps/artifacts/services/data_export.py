"""CSV export of the data behind an artifact (#846).

Each request exports one dataset: a live semantic query the artifact declares,
or a tabular entry of its static ``data``. One query result is held in memory
at a time; a zip of every dataset would hold all of them or need sequential
Cube round-trips inside a single long response.
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Any

from apps.semantic.services.query import run_semantic_query

from .query_batch import ArtifactQueryPlan, PlannedQuery

# Cube's default CUBEJS_DB_QUERY_LIMIT; a larger limit is refused by Cube itself.
EXPORT_ROW_LIMIT = 50_000
# Cube's own budget is 60s (QUERY_TOTAL_TIMEOUT_SECONDS); this also bounds
# catalog compilation and the workspace-context lookup.
EXPORT_TIMEOUT_SECONDS = 90
CSV_CHUNK_ROWS = 1_000

QUERY_SOURCE = "query"
STATIC_SOURCE = "static"

# Spreadsheet apps evaluate a cell starting with these as a formula (OWASP CSV injection).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_PLAIN_NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")


@dataclass(frozen=True)
class TabularData:
    columns: list[str]
    rows: list[list[Any]]


def csv_safe_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool | int | float):
        return value
    text = value if isinstance(value, str) else str(value)
    # Cube returns numeric measures as strings; a negative number is data, not a formula.
    if text.startswith(_FORMULA_PREFIXES) and not _PLAIN_NUMBER.fullmatch(text):
        return "'" + text
    return text


async def iter_csv(columns: Iterable[Any], rows: Iterable[Iterable[Any]]) -> AsyncIterator[bytes]:
    """Yield an RFC 4180 CSV (CRLF, minimal quoting) with a UTF-8 BOM for Excel."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    buffer.write("﻿")
    writer.writerow([csv_safe_cell(column) for column in columns])
    pending = 0
    for row in rows:
        writer.writerow([csv_safe_cell(cell) for cell in row])
        pending += 1
        if pending >= CSV_CHUNK_ROWS:
            yield buffer.getvalue().encode()
            buffer.seek(0)
            buffer.truncate()
            pending = 0
            # Let other requests on this event loop run between chunks.
            await asyncio.sleep(0)
    remainder = buffer.getvalue()
    if remainder:
        yield remainder.encode()


def static_tabular_datasets(data: Any) -> dict[str, TabularData]:
    """Entries of ``artifact.data`` that are tables: a list of records, or columns + rows."""
    if not isinstance(data, dict):
        return {}
    tables = {}
    for key, value in data.items():
        table = _as_table(value)
        if table is not None:
            tables[str(key)] = table
    return tables


def _as_table(value: Any) -> TabularData | None:
    if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
        columns: list[str] = []
        seen: set[str] = set()
        for record in value:
            for column in record:
                if column not in seen:
                    seen.add(column)
                    columns.append(column)
        return TabularData(
            columns, [[record.get(column) for column in columns] for record in value]
        )
    if (
        isinstance(value, dict)
        and isinstance(value.get("columns"), list)
        and isinstance(value.get("rows"), list)
        and all(isinstance(row, list) for row in value["rows"])
    ):
        return TabularData([str(c) for c in value["columns"]], value["rows"])
    return None


def export_datasets(plan: ArtifactQueryPlan, data: Any) -> list[dict[str, str]]:
    seen: set[str] = set()
    datasets = []
    for planned in plan.queries:
        if planned.spec is not None and planned.name not in seen:
            seen.add(planned.name)
            datasets.append({"name": planned.name, "source": QUERY_SOURCE})
    datasets.extend(
        {"name": name, "source": STATIC_SOURCE} for name in static_tabular_datasets(data)
    )
    return datasets


def find_planned_query(plan: ArtifactQueryPlan, name: str) -> PlannedQuery | None:
    return next(
        (q for q in plan.queries if q.name == name and q.spec is not None),
        None,
    )


async def run_export_query(workspace, planned: PlannedQuery, *, user_id: str) -> dict[str, Any]:
    """Run one declared query with the export cap in place of its display limit.

    The artifact's own ``limit`` sizes a chart, not the data behind it, so it is
    replaced. Raises ``TimeoutError`` past ``EXPORT_TIMEOUT_SECONDS``.
    """
    spec = {**planned.spec, "limit": EXPORT_ROW_LIMIT}
    async with asyncio.timeout(EXPORT_TIMEOUT_SECONDS):
        return await run_semantic_query(
            workspace, spec, user_id=user_id, max_limit=EXPORT_ROW_LIMIT
        )


def export_filename(title: str, dataset: str, *, truncated: bool) -> str:
    stem = "-".join(part for part in (_slug(title), _slug(dataset)) if part) or "artifact-data"
    if truncated:
        stem += f"-first-{EXPORT_ROW_LIMIT}-rows"
    return f"{stem}.csv"


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value or "").strip("-").lower()[:60]
