"""Readiness classifications survive semantic execution, graph checks, and handoff."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import ToolMessage

from apps.agents.tools.artifact_manager_agent import _summarize_result
from apps.artifacts.services import graph_runtime
from apps.semantic.models import SemanticDataset, SemanticField, SemanticModel
from apps.semantic.services import query as query_service
from apps.semantic.services import query_outcomes
from apps.semantic.services.catalog import SemanticCatalogUnavailable
from apps.semantic.services.query import run_semantic_query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "surface,category,action",
    [
        (
            {"status": "model_drift", "queryable": False, "recovery_action": None},
            "data_unavailable",
            None,
        ),
        (
            {
                "status": "needs_materialization",
                "queryable": False,
                "recovery_action": "materialization",
            },
            "data_unavailable",
            "materialization",
        ),
        (
            {"status": "needs_view_rebuild", "queryable": False, "recovery_action": "view_rebuild"},
            "data_unavailable",
            "view_rebuild",
        ),
        (
            {
                "status": "needs_semantic_rebuild",
                "queryable": False,
                "recovery_action": "semantic_rebuild",
            },
            "data_unavailable",
            "semantic_rebuild",
        ),
        (
            {"status": "recovering", "queryable": False, "recovery_action": None},
            "data_unavailable",
            None,
        ),
        ({"status": "ready", "queryable": True, "recovery_action": None}, "invalid_query", None),
    ],
)
async def test_query_uses_artifact_readiness_authority(monkeypatch, surface, category, action):
    inspect = AsyncMock(return_value=surface)
    monkeypatch.setattr(query_outcomes, "artifact_query_surface", inspect)
    workspace = SimpleNamespace(id="workspace")
    query = {"measures": ["visits.count"]}
    result = await query_outcomes.query_readiness_error(
        workspace, query, "VALIDATION_ERROR", "Failed", category="invalid_query"
    )
    subject = inspect.await_args.args[0]
    assert subject.workspace is workspace
    assert subject.semantic_queries == [query]
    assert subject.id is None
    assert result["error"]["category"] == category
    assert result["error"]["recovery_action"] == action
    assert result["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_readiness_failure_does_not_guess_a_repair(monkeypatch):
    monkeypatch.setattr(
        query_outcomes, "artifact_query_surface", AsyncMock(side_effect=RuntimeError("Unavailable"))
    )
    result = await query_outcomes.query_readiness_error(
        SimpleNamespace(id="workspace"),
        {},
        "VALIDATION_ERROR",
        "Original",
        category="missing_model_dependency",
    )
    assert result["error"]["message"] == "Original"
    assert result["error"]["recovery_action"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "surface,category,action",
    [
        ({"status": "ready", "queryable": True}, "invalid_query", None),
        (
            {
                "status": "needs_semantic_rebuild",
                "queryable": False,
                "recovery_action": "semantic_rebuild",
            },
            "data_unavailable",
            "semantic_rebuild",
        ),
        (
            {
                "status": "needs_materialization",
                "queryable": False,
                "recovery_action": "materialization",
            },
            "data_unavailable",
            "materialization",
        ),
        ({"status": "model_drift", "queryable": False}, "data_unavailable", None),
    ],
)
async def test_cube_rejection_checks_serving_readiness_without_inventing_model_gaps(
    monkeypatch, surface, category, action
):
    workspace = SimpleNamespace(id="workspace")
    query = {"measures": ["visits.count"]}
    monkeypatch.setattr(
        query_service,
        "_compile_semantic_query_for_async",
        lambda *args: {
            "model": object(),
            "cube_schema": object(),
            "cube_query": {},
        },
    )
    monkeypatch.setattr(query_service, "load_workspace_context", AsyncMock(return_value=object()))
    monkeypatch.setattr(query_service, "build_cube_security_context", lambda *args, **kwargs: {})
    execute = AsyncMock(side_effect=query_service.CubeQueryError("Member not found: visits.count"))
    monkeypatch.setattr(query_service, "CubeClient", lambda: SimpleNamespace(execute_query=execute))
    inspect = AsyncMock(return_value=surface)
    monkeypatch.setattr(query_outcomes, "artifact_query_surface", inspect)
    result = await run_semantic_query(workspace, query)
    execute.assert_awaited_once()
    inspect.assert_awaited_once()
    assert inspect.await_args.args[0].semantic_queries == [query]
    assert result["error"]["category"] == category
    assert result["error"]["recovery_action"] == action
    assert result["error"]["retryable"] is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_expired_data_and_missing_member_are_distinct(workspace):
    empty = await run_semantic_query(workspace, {"measures": ["visits.count"]})
    assert empty["error"]["category"] == "data_unavailable"
    assert empty["error"]["recovery_action"] == "materialization"
    model = await SemanticModel.objects.acreate(workspace=workspace, name="Visits")
    dataset = await SemanticDataset.objects.acreate(
        workspace=workspace, semantic_model=model, name="visits", table_name="raw_visits"
    )
    await SemanticField.objects.acreate(
        dataset=dataset, name="count", field_type="measure", measure_type="count", expression="*"
    )
    missing = await run_semantic_query(workspace, {"measures": ["visits.deleted"]})
    assert missing["error"]["category"] == "missing_model_dependency"
    assert missing["error"]["recovery_action"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_status", ["error", "checked"])
@pytest.mark.parametrize(
    "code,category,retryable,action",
    [
        ("CONNECTION_ERROR", "transient_runtime_failure", True, None),
        ("VALIDATION_ERROR", "missing_model_dependency", False, None),
        ("VALIDATION_ERROR", "data_unavailable", False, "view_rebuild"),
        ("AUTH_ACCESS_DENIED", "permission_required", False, None),
    ],
)
async def test_graph_and_parent_preserve_failure_classification(
    monkeypatch, code, category, retryable, action, tool_status
):
    monkeypatch.setattr(graph_runtime.Workspace.objects, "aget", AsyncMock(return_value=object()))
    error = {
        "code": code,
        "message": "Failure",
        "category": category,
        "retryable": retryable,
        "recovery_action": action,
    }
    monkeypatch.setattr(
        graph_runtime,
        "run_semantic_query",
        AsyncMock(return_value={"success": False, "error": error}),
    )
    artifact = SimpleNamespace(
        workspace_id="workspace",
        data={
            "story_doc": {
                "schema_version": 1,
                "name": "Query",
                "blocks": [
                    {
                        "id": "q",
                        "type": "semantic_query",
                        "hidden": True,
                        "config": {"queries": {"count": {"measures": ["visits.count"]}}},
                    }
                ],
            }
        },
    )
    runtime = await graph_runtime.check_graph_artifact(artifact)
    assert runtime["queries"][0]["failure"] == error
    result = {"status": tool_status, "runtime": runtime}
    messages = [
        ToolMessage(name="artifact_write", tool_call_id="write", content=json.dumps(result))
    ]
    summary = _summarize_result(
        messages, json.dumps({"status": "done", "message": "Incorrect success claim"})
    )
    assert summary["status"] == "error"
    assert summary["runtime_failures"] == [{"query_key": "q.count", **error}]


@pytest.mark.asyncio
async def test_invalid_document_never_runs_queries(monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(graph_runtime, "run_semantic_query", execute)
    artifact = SimpleNamespace(data={"story_doc": {"schema_version": 1, "blocks": []}})
    result = await graph_runtime.check_graph_artifact(artifact)
    execute.assert_not_awaited()
    assert result["failures"][0]["category"] == "invalid_document"
    assert result["failures"][0]["message"] == result["summary"]
    assert result["failures"][0]["query_key"] is None
    assert result["manifest"]["entry_count"] == 0


@pytest.mark.asyncio
async def test_batch_drift_cannot_turn_a_catalog_outage_into_a_model_edit(monkeypatch):
    def unavailable(*args):
        raise SemanticCatalogUnavailable("Catalog unavailable")

    workspace = SimpleNamespace(id="workspace")
    monkeypatch.setattr(graph_runtime.Workspace.objects, "aget", AsyncMock(return_value=workspace))
    monkeypatch.setattr(
        "apps.semantic.services.query._compile_semantic_query_for_async",
        unavailable,
    )
    inspect = AsyncMock(return_value={"status": "model_drift", "queryable": False})
    monkeypatch.setattr(query_outcomes, "artifact_query_surface", inspect)
    artifact = SimpleNamespace(
        workspace_id="workspace",
        data={
            "story_doc": {
                "schema_version": 1,
                "blocks": [
                    {
                        "id": "q",
                        "type": "semantic_query",
                        "config": {
                            "queries": {
                                "valid": {"measures": ["visits.count"]},
                                "stale": {"measures": ["visits.deleted"]},
                            }
                        },
                    }
                ],
            }
        },
    )
    runtime = await graph_runtime.check_graph_artifact(artifact)
    assert {failure["category"] for failure in runtime["failures"]} == {"data_unavailable"}
    inspect.assert_awaited_once()
    summary = _summarize_result(
        [
            ToolMessage(
                name="artifact_write",
                tool_call_id="check",
                content=json.dumps(
                    {
                        "status": "checked",
                        "runtime": runtime,
                    }
                ),
            )
        ],
        json.dumps(
            {"status": "needs_data_model", "data_requirements": ["Invent a replacement field"]}
        ),
    )
    assert summary["status"] == "error"
    assert "data_requirements" not in summary


@pytest.mark.parametrize("failures", [None, {}, "failure", 42])
def test_failed_check_never_claims_success_with_malformed_failure_details(failures):
    result = {"status": "checked", "runtime": {"success": False, "failures": failures}}
    summary = _summarize_result(
        [ToolMessage(name="artifact_write", tool_call_id="check", content=json.dumps(result))],
        json.dumps({"status": "done", "message": "Incorrect success claim"}),
    )
    assert summary["status"] == "error"
    assert "runtime_failures" not in summary


def test_missing_error_detail_uses_a_readable_failure_message():
    assert graph_runtime._query_failure(None)["message"] == "Semantic query failed"


@pytest.mark.parametrize("status", ["checked", "error"])
@pytest.mark.parametrize(
    "static_diagnostics", [None, [], [{"severity": "warning", "code": "layout"}]]
)
def test_failed_check_preserves_runtime_document_diagnostics(status, static_diagnostics):
    diagnostics = [{"severity": "error", "code": "missing_block", "path": "blocks.0"}]
    result = {
        "status": status,
        "diagnostics": static_diagnostics,
        "runtime": {"success": False, "diagnostics": diagnostics},
    }
    summary = _summarize_result(
        [ToolMessage(name="artifact_write", tool_call_id="check", content=json.dumps(result))],
        json.dumps({"status": "done", "message": "Incorrect success claim"}),
    )
    assert summary["status"] == "error"
    assert summary["diagnostics"] == (static_diagnostics or []) + diagnostics


def test_bounded_handoff_keeps_a_late_failure_with_a_different_cause():
    failures = [
        {"category": "missing_model_dependency", "message": f"Missing {i}"} for i in range(9)
    ]
    permission = {"category": "permission_required", "message": "Access must be restored"}
    failures.append(permission)
    result = {"status": "checked", "runtime": {"success": False, "failures": failures}}
    summary = _summarize_result(
        [ToolMessage(name="artifact_write", tool_call_id="check", content=json.dumps(result))],
        json.dumps({"status": "needs_data_model", "data_requirements": ["A dimension"]}),
    )
    assert summary["status"] == "error"
    assert len(summary["runtime_failures"]) == 8
    assert permission in summary["runtime_failures"]
    assert summary["runtime_failures"][0] == failures[0]


@pytest.mark.parametrize("claimed_status", ["done", "needs_data_model"])
def test_static_artifact_failure_cannot_be_overridden_by_model_output(claimed_status):
    result = {
        "status": "error",
        "message": "Graph doc has validation errors.",
        "diagnostics": [{"severity": "error", "code": "invalid_document"}],
    }
    summary = _summarize_result(
        [ToolMessage(name="artifact_write", tool_call_id="write", content=json.dumps(result))],
        json.dumps({"status": claimed_status, "message": "Claim", "data_requirements": ["Claim"]}),
    )
    assert summary["status"] == "error"
    assert summary["message"] == result["message"]
    assert summary["diagnostics"] == result["diagnostics"]
    assert "data_requirements" not in summary


@pytest.mark.parametrize("status,artifact_id", [("error", None), ("checked", "existing")])
def test_failed_write_never_exposes_a_soft_deleted_candidate(status, artifact_id):
    result = {
        "status": status,
        "artifact": {"id": "existing", "version": 2},
        "runtime": {"success": False, "failures": [{"category": "missing_model_dependency"}]},
    }
    messages = [
        ToolMessage(name="artifact_write", tool_call_id="write", content=json.dumps(result))
    ]
    summary = _summarize_result(messages, json.dumps({"status": "error"}))
    assert summary["artifact_id"] == artifact_id
    assert summary["artifact_version"] == (2 if artifact_id else None)


@pytest.mark.asyncio
async def test_failed_readiness_inspection_is_cached_and_logged_once(monkeypatch, caplog):
    inspect = AsyncMock(side_effect=RuntimeError("Unavailable"))
    monkeypatch.setattr(query_outcomes, "artifact_query_surface", inspect)
    workspace = SimpleNamespace(id="workspace")
    readiness = query_outcomes.QueryReadiness(workspace, [{}])
    results = await asyncio.gather(
        *[
            query_outcomes.query_readiness_error(
                workspace,
                {},
                "VALIDATION_ERROR",
                "Original",
                category="invalid_query",
                readiness=readiness,
            )
            for _ in range(3)
        ]
    )
    inspect.assert_awaited_once()
    assert (
        sum("Unable to inspect query readiness" in record.message for record in caplog.records) == 1
    )
    assert all(result["error"]["category"] == "invalid_query" for result in results)
    assert all(result["error"]["recovery_action"] is None for result in results)


@pytest.mark.asyncio
async def test_batch_readiness_is_inspected_once_and_not_shared_between_requests(monkeypatch):
    inspect = AsyncMock(
        return_value={
            "status": "needs_materialization",
            "queryable": False,
            "recovery_action": "materialization",
        }
    )
    monkeypatch.setattr(query_outcomes, "artifact_query_surface", inspect)
    workspace = SimpleNamespace(id="workspace")
    queries = [{"measures": ["visits.count"]}, {"measures": ["forms.count"]}]
    readiness = query_outcomes.QueryReadiness(workspace, queries)
    await asyncio.gather(
        *[
            query_outcomes.query_readiness_error(
                workspace,
                query,
                "VALIDATION_ERROR",
                "Unavailable",
                category="data_unavailable",
                readiness=readiness,
            )
            for query in queries
        ]
    )
    inspect.assert_awaited_once()
    assert inspect.await_args.args[0].semantic_queries == queries
    await query_outcomes.QueryReadiness(workspace, queries).surface()
    assert inspect.await_count == 2


@pytest.mark.asyncio
async def test_warning_only_document_still_executes_queries(monkeypatch):
    monkeypatch.setattr(
        graph_runtime,
        "validate_doc",
        lambda doc: [{"severity": "warning", "code": "unknown_input"}],
    )
    monkeypatch.setattr(graph_runtime.Workspace.objects, "aget", AsyncMock(return_value=object()))
    execute = AsyncMock(return_value={"columns": [], "rows": [], "row_count": 0})
    monkeypatch.setattr(graph_runtime, "run_semantic_query", execute)
    artifact = SimpleNamespace(
        workspace_id="workspace",
        data={
            "story_doc": {
                "schema_version": 1,
                "name": "Warnings",
                "blocks": [
                    {
                        "id": "q",
                        "type": "semantic_query",
                        "config": {"queries": {"count": {"measures": ["visits.count"]}}},
                    }
                ],
            }
        },
    )
    result = await graph_runtime.check_graph_artifact(artifact)
    execute.assert_awaited_once()
    assert result["success"] is True
    assert result["diagnostics"][0]["severity"] == "warning"
