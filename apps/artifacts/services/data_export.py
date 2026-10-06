"""CSV export of the data behind an artifact (#846).

Each request exports one dataset: a live semantic query the artifact declares,
or a tabular entry of its static ``data``. Cube's /load cannot stream, so the
whole result (up to EXPORT_ROW_LIMIT rows) is in memory before the first byte;
only the CSV encoding is chunked. Memory is bounded by one dataset per request
and MAX_CONCURRENT_EXPORTS per process, not by streaming. A zip of every
dataset would hold all of them at once.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Any

from django.http import JsonResponse

from apps.semantic.services.query import run_semantic_query

from .query_batch import ArtifactQueryPlan, PlannedQuery

# Cube's default CUBEJS_DB_QUERY_LIMIT; a larger limit is refused by Cube itself.
EXPORT_ROW_LIMIT = 50_000
# Cube's own budget is 60s (QUERY_TOTAL_TIMEOUT_SECONDS); this also bounds
# catalog compilation and the workspace-context lookup.
EXPORT_TIMEOUT_SECONDS = 90
CSV_CHUNK_ROWS = 1_000
# Per process: an export holds a tenant Cube connection slot and up to 50k rows
# in memory, so a burst of exports must not starve dashboard queries.
MAX_CONCURRENT_EXPORTS = 2
EXPORT_TIMEOUT_MESSAGE = "The export took too long. Add filters to narrow it and try again."

QUERY_SOURCE = "query"
STATIC_SOURCE = "static"

# Spreadsheet apps evaluate a cell starting with these as a formula (OWASP CSV injection).
# Fullwidth forms are evaluated as formulas by some Excel locales.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\uff1d", "\uff0b", "\uff0d", "\uff20")
_PLAIN_NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?", re.ASCII)


@dataclass(frozen=True)
class TabularData:
    columns: list[str]
    rows: list[list[Any]]


def csv_safe_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return value
    if isinstance(value, dict | list):
        text = json.dumps(value, default=str)
    else:
        text = value if isinstance(value, str) else str(value)
    # Some importers trim before evaluating, so leading whitespace does not hide a formula.
    # Cube returns numeric measures as strings; a negative number is data, not a formula.
    if text.lstrip(" \n").startswith(_FORMULA_PREFIXES) and not _PLAIN_NUMBER.fullmatch(text):
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


class _ExportSlot:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        global _active_exports
        _active_exports -= 1


_active_exports = 0


def export_slot() -> _ExportSlot | None:
    """Claim one of this process's export slots, or None when all are busy.

    A plain counter rather than an asyncio.Semaphore: it never waits, so it is not
    bound to an event loop, and a full house is answered at once with a busy 503.
    """
    global _active_exports
    if _active_exports >= MAX_CONCURRENT_EXPORTS:
        return None
    _active_exports += 1
    return _ExportSlot()


def export_error_response(error: Any) -> JsonResponse:
    if not isinstance(error, dict):
        return JsonResponse({"error": str(error) or "The export query failed."}, status=502)
    category = error.get("category")
    message = error.get("message") or "The export query failed."
    if category == "data_unavailable":
        return JsonResponse({"error": message}, status=409)
    if category in {"transient_runtime_failure", "runtime_failure"}:
        if "timed out" in message:
            return JsonResponse({"error": EXPORT_TIMEOUT_MESSAGE}, status=504)
        return JsonResponse({"error": message}, status=503)
    return JsonResponse({"error": message}, status=502)


def audit_query_shape(query: Any) -> dict[str, Any]:
    """What was exported, without filter values: those can carry personal identifiers."""
    if not isinstance(query, dict):
        return {}
    return {
        "measures": query.get("measures"),
        "dimensions": query.get("dimensions"),
        "time_dimension": query.get("time_dimension"),
        "granularity": query.get("granularity"),
        "filters": [
            {"field": f.get("field") or f.get("member"), "operator": f.get("operator")}
            for f in query.get("filters") or []
            if isinstance(f, dict)
        ],
        "limit": query.get("limit"),
    }


def export_filename(title: str, dataset: str, *, truncated: bool) -> str:
    stem = "-".join(part for part in (_slug(title), _slug(dataset)) if part) or "artifact-data"
    if truncated:
        stem += f"-first-{EXPORT_ROW_LIMIT}-rows"
    return f"{stem}.csv"


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value or "").strip("-").lower()[:60]
