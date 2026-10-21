"""CSV export of the full result behind a chat query card (#969).

The agent sees at most 500 rows of a ``query`` or ``semantic_query`` result, and
those rows are paid for again on every later turn. A download re-runs the same
tool call under the artifact export's row cap, lease and CSV encoding, so a
large result reaches the user without passing through the model.

The call is read back from the thread's own checkpoint, never from the request:
the endpoint exports what the agent already ran for this user, and is not a way
to send the database arbitrary SQL.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, replace
from typing import Any

from asgiref.sync import sync_to_async
from django.db import connections
from django.http import JsonResponse

from apps.agents.graph.state import all_tool_calls
from apps.artifacts.services.data_export import (
    EXPORT_ROW_LIMIT,
    EXPORT_TIMEOUT_MESSAGE,
    EXPORT_TIMEOUT_SECONDS,
    ExportLease,
    LeasedStreamingResponse,
    audit_query_shape,
    export_filename,
    iter_csv,
    leased_stream,
)
from apps.common.capacity import busy_response
from apps.semantic.services.query import (
    MAX_SEMANTIC_LIMIT,
    run_semantic_query,
    semantic_tool_spec,
)
from mcp_server.context import load_workspace_context
from mcp_server.envelope import CAPACITY_EXHAUSTED, CONNECTION_ERROR, QUERY_TIMEOUT
from mcp_server.services.query import execute_query

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("scout.export.audit")

SQL_TOOL = "query"
SEMANTIC_TOOL = "semantic_query"
EXPORTABLE_TOOLS = frozenset({SQL_TOOL, SEMANTIC_TOOL})
# The chat tool's 30s fits 500 rows; the whole result can take longer to read.
SQL_EXPORT_STATEMENT_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class ExportableCall:
    name: str
    args: dict[str, Any]


def find_exportable_call(messages: list[Any], tool_call_id: str) -> ExportableCall | None:
    """The ``query`` or ``semantic_query`` call ``tool_call_id`` names in a thread's history."""
    for message in messages:
        if getattr(message, "type", None) != "ai":
            continue
        for call in all_tool_calls(message):
            if call.get("id") != tool_call_id:
                continue
            args = call.get("args")
            if call.get("name") not in EXPORTABLE_TOOLS or not isinstance(args, dict):
                return None
            return ExportableCall(call["name"], args)
    return None


def semantic_export_spec(args: dict[str, Any]) -> dict[str, Any]:
    """The tool's spec with the display cap lifted, keeping the limit the agent chose.

    A "top 10" exports 10 rows and a "top 1000" 1000, as a SQL LIMIT does; a limit
    of exactly the cap is the agent asking for all it may see, so it gets the export cap.
    """
    spec = semantic_tool_spec(args)
    try:
        requested = int(spec["limit"])
    except (TypeError, ValueError):
        requested = 100  # _coerce_limit's default, which the chat call ran with
    if requested == MAX_SEMANTIC_LIMIT:
        requested = EXPORT_ROW_LIMIT
    spec["limit"] = max(1, min(requested, EXPORT_ROW_LIMIT))
    return spec


class DataUnavailable(Exception):
    """The workspace has no schema to read, so the call cannot be re-run."""


async def run_full_result(workspace, call: ExportableCall, *, user_id: str) -> dict[str, Any]:
    """Re-run ``call`` for export. Raises ``TimeoutError`` past ``EXPORT_TIMEOUT_SECONDS``,
    and ``DataUnavailable`` when the workspace has no data to read."""
    async with asyncio.timeout(EXPORT_TIMEOUT_SECONDS):
        if call.name == SQL_TOOL:
            try:
                ctx = await load_workspace_context(str(workspace.id))
            except ValueError as exc:
                # DataNotLoaded included: expired data, or a workspace with no sources left.
                raise DataUnavailable(str(exc)) from exc
            ctx = replace(
                ctx,
                max_rows_per_query=EXPORT_ROW_LIMIT,
                max_query_timeout_seconds=SQL_EXPORT_STATEMENT_TIMEOUT_SECONDS,
            )
            return await execute_query(ctx, str(call.args.get("sql") or ""))
        return await run_semantic_query(
            workspace,
            semantic_export_spec(call.args),
            user_id=user_id,
            max_limit=EXPORT_ROW_LIMIT,
        )


def sql_error_response(error: Any) -> JsonResponse:
    """Map an MCP error envelope's error to the export's HTTP answer."""
    code = error.get("code") if isinstance(error, dict) else None
    message = (error.get("message") if isinstance(error, dict) else None) or (
        "The export query failed."
    )
    if code == CAPACITY_EXHAUSTED:
        # execute_query has already raised the capacity alert.
        return busy_response()
    if code == QUERY_TIMEOUT:
        return JsonResponse({"error": EXPORT_TIMEOUT_MESSAGE}, status=504)
    if code == CONNECTION_ERROR:
        return JsonResponse({"error": message}, status=503)
    # The query ran in chat, so a refusal now means the data changed under it.
    return JsonResponse({"error": message}, status=502)


def _audit_query(call: ExportableCall) -> str:
    if call.name == SQL_TOOL:
        # The SQL can carry literal identifiers; a digest still ties the export to its call.
        sql = str(call.args.get("sql") or "")
        return "sql_sha256=" + hashlib.sha256(sql.encode()).hexdigest()[:16]
    return json.dumps(audit_query_shape(semantic_tool_spec(call.args)), sort_keys=True)


async def csv_response(
    *,
    user,
    thread,
    tool_call_id: str,
    call: ExportableCall,
    result: dict[str, Any],
    lease: ExportLease,
) -> LeasedStreamingResponse:
    columns = result.get("columns", [])
    rows = result.get("rows", [])
    truncated = bool(result.get("truncated"))
    # Otherwise the request's connection stays open until request_finished, which
    # under ASGI fires after the last streamed byte (see ArtifactDataExportCsvView).
    await sync_to_async(connections.close_all)()
    audit_logger.info(
        "Chat query export started: user=%s workspace=%s thread=%s tool_call=%r tool=%s "
        "rows=%d truncated=%s query=%s",
        user.id,
        thread.workspace_id,
        thread.id,
        tool_call_id,
        call.name,
        len(rows),
        truncated,
        _audit_query(call),
    )
    filename = export_filename(thread.title, call.name, truncated=truncated)
    response = LeasedStreamingResponse(
        leased_stream(iter_csv(columns, rows), lease),
        content_type="text/csv; charset=utf-8",
        lease=lease,
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "no-store"
    response["X-Scout-Export-Row-Count"] = str(len(rows))
    response["X-Scout-Export-Row-Limit"] = str(EXPORT_ROW_LIMIT)
    response["X-Scout-Export-Truncated"] = "true" if truncated else "false"
    return response
