"""Tests for the full-result CSV export of a chat query card (#969)."""

import logging
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from apps.artifacts.services import data_export
from apps.artifacts.services.data_export import EXPORT_ROW_LIMIT
from apps.artifacts.tests.test_artifact_data_export import (
    _body,
    _client,
    _member,
    _rows,
    _workspace,
)
from apps.chat.models import Thread
from apps.chat.query_export import semantic_export_spec
from apps.common.errors import DataNotLoaded
from apps.semantic.services.query import MAX_SEMANTIC_LIMIT
from apps.workspaces.models import WorkspaceRole
from mcp_server.context import QueryContext
from mcp_server.envelope import CAPACITY_EXHAUSTED, QUERY_TIMEOUT, error_response

MESSAGES = "apps.chat.thread_views._load_checkpoint_messages"
CONTEXT = "apps.chat.query_export.load_workspace_context"
EXECUTE = "apps.chat.query_export.execute_query"
SEMANTIC = "apps.chat.query_export.run_semantic_query"

SQL = "SELECT llo, bucket, count(*) FROM visits WHERE owner = 'a@example.com' GROUP BY 1, 2"


@pytest.fixture(autouse=True)
def no_leaked_export_leases():
    yield
    leaked = data_export.active_exports()
    data_export._active_leases.clear()
    assert leaked == 0


@pytest.fixture
def workspace(db):
    return _workspace("query-export-domain")


@pytest.fixture
def writer(workspace):
    return _member(workspace, "writer@example.com", WorkspaceRole.READ_WRITE)


@pytest.fixture
def thread(workspace, writer):
    return Thread.objects.create(workspace=workspace, user=writer, title="KMC intervals")


@pytest.fixture
def writer_client(writer):
    return _client(writer)


@pytest.fixture
def reader_client(workspace):
    return _client(_member(workspace, "reader@example.com", WorkspaceRole.READ))


@pytest.fixture
def other_manager_client(workspace):
    return _client(_member(workspace, "other@example.com", WorkspaceRole.MANAGE))


def _history(name="query", args=None, call_id="call_1"):
    return [
        HumanMessage(content="days between visits by LLO"),
        AIMessage(
            content="",
            tool_calls=[{"id": call_id, "name": name, "args": args or {"sql": SQL}}],
        ),
    ]


def _url(workspace, thread, call_id="call_1"):
    return f"/api/workspaces/{workspace.id}/threads/{thread.id}/tool-calls/{call_id}/export/csv/"


def _sql_result(rows, *, truncated=False):
    return {
        "columns": ["llo", "bucket", "count"],
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "row_limit": EXPORT_ROW_LIMIT,
        "sql_executed": SQL,
        "tables_accessed": ["visits"],
    }


def _context():
    return QueryContext(tenant_id="t", schema_name="t_query_export", connection_params={})


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_writer_downloads_the_full_sql_result_of_a_chat_query(
    workspace, thread, writer_client, caplog
):
    rows = [["LLO A", str(day), 3] for day in range(700)]
    execute = AsyncMock(return_value=_sql_result(rows))
    with (
        patch(MESSAGES, new=AsyncMock(return_value=_history())),
        patch(CONTEXT, new=AsyncMock(return_value=_context())),
        patch(EXECUTE, new=execute),
        caplog.at_level(logging.INFO, logger="scout.export.audit"),
    ):
        response = await writer_client.post(_url(workspace, thread))
        body = await _body(response)

    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["X-Scout-Export-Truncated"] == "false"
    assert response["X-Scout-Export-Row-Count"] == "700"
    assert response["Content-Disposition"] == 'attachment; filename="kmc-intervals-query.csv"'
    parsed = _rows(body)
    assert parsed[0] == ["llo", "bucket", "count"]
    assert len(parsed) == 701

    ctx, sql = execute.await_args.args
    assert sql == SQL
    assert ctx.max_rows_per_query == EXPORT_ROW_LIMIT
    assert ctx.max_query_timeout_seconds > _context().max_query_timeout_seconds
    # The SQL's literals can identify people; the audit line carries a digest instead.
    assert "a@example.com" not in caplog.text
    assert "sql_sha256=" in caplog.text


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_an_export_cut_at_the_export_cap_says_so(workspace, thread, writer_client):
    with (
        patch(MESSAGES, new=AsyncMock(return_value=_history())),
        patch(CONTEXT, new=AsyncMock(return_value=_context())),
        patch(EXECUTE, new=AsyncMock(return_value=_sql_result([["a", "1", 1]], truncated=True))),
    ):
        response = await writer_client.post(_url(workspace, thread))
        await _body(response)

    assert response["X-Scout-Export-Truncated"] == "true"
    assert f"first-{EXPORT_ROW_LIMIT}-rows" in response["Content-Disposition"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_read_members_cannot_bulk_export(workspace, thread, reader_client):
    execute = AsyncMock()
    with patch(MESSAGES, new=AsyncMock(return_value=_history())), patch(EXECUTE, new=execute):
        response = await reader_client.post(_url(workspace, thread))
    assert response.status_code == 403
    execute.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_another_users_thread_is_not_found(workspace, thread, other_manager_client):
    execute = AsyncMock()
    with patch(MESSAGES, new=AsyncMock(return_value=_history())), patch(EXECUTE, new=execute):
        response = await other_manager_client.post(_url(workspace, thread))
    assert response.status_code == 404
    execute.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("history", "call_id"),
    [
        (_history(), "call_missing"),
        (_history(name="describe_table", args={"table_name": "visits"}), "call_1"),
    ],
)
async def test_only_a_query_call_from_this_thread_exports(
    workspace, thread, writer_client, history, call_id
):
    execute = AsyncMock()
    with patch(MESSAGES, new=AsyncMock(return_value=history)), patch(EXECUTE, new=execute):
        response = await writer_client.post(_url(workspace, thread, call_id))
    assert response.status_code == 404
    execute.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_export_is_post_only(workspace, thread, writer_client):
    response = await writer_client.get(_url(workspace, thread))
    assert response.status_code == 405


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status"),
    [(QUERY_TIMEOUT, 504), (CAPACITY_EXHAUSTED, 503)],
)
async def test_sql_failures_map_to_http_errors(workspace, thread, writer_client, code, status):
    with (
        patch(MESSAGES, new=AsyncMock(return_value=_history())),
        patch(CONTEXT, new=AsyncMock(return_value=_context())),
        patch(EXECUTE, new=AsyncMock(return_value=error_response(code, "nope"))),
    ):
        response = await writer_client.post(_url(workspace, thread))
    assert response.status_code == status


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_workspace_whose_data_expired_answers_conflict(workspace, thread, writer_client):
    execute = AsyncMock()
    with (
        patch(MESSAGES, new=AsyncMock(return_value=_history())),
        patch(CONTEXT, new=AsyncMock(side_effect=DataNotLoaded("No active data schema."))),
        patch(EXECUTE, new=execute),
    ):
        response = await writer_client.post(_url(workspace, thread))
    assert response.status_code == 409
    assert response.json()["error"] == "No active data schema."
    execute.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_semantic_query_export_lifts_the_display_cap(workspace, thread, writer_client):
    args = {"measures": ["visits.count"], "dimensions": ["visits.llo"], "limit": 500}
    run = AsyncMock(
        return_value={
            "columns": ["visits.llo", "visits.count"],
            "rows": [["A", "3"]],
            "row_count": 1,
            "truncated": False,
        }
    )
    with (
        patch(MESSAGES, new=AsyncMock(return_value=_history("semantic_query", args))),
        patch(SEMANTIC, new=run),
    ):
        response = await writer_client.post(_url(workspace, thread))
        body = await _body(response)

    assert response.status_code == 200
    assert _rows(body) == [["visits.llo", "visits.count"], ["A", "3"]]
    spec = run.await_args.args[1]
    assert spec["limit"] == EXPORT_ROW_LIMIT
    assert spec["dimensions"] == ["visits.llo"]
    assert run.await_args.kwargs["max_limit"] == EXPORT_ROW_LIMIT


@pytest.mark.parametrize(
    ("limit", "exported"),
    [
        (MAX_SEMANTIC_LIMIT, EXPORT_ROW_LIMIT),
        (1_000, 1_000),
        (10**6, EXPORT_ROW_LIMIT),
        (10, 10),
        ("lots", 100),
    ],
)
def test_semantic_export_keeps_a_limit_the_agent_chose(limit, exported):
    assert semantic_export_spec({"measures": ["visits.count"], "limit": limit})["limit"] == exported


def test_semantic_export_drops_a_replayed_clock():
    spec = semantic_export_spec(
        {"query_context": {"timezone": "UTC", "now": "2020-01-01T00:00:00Z"}}
    )
    assert spec["query_context"] == {"timezone": "UTC"}
