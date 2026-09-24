from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.artifacts.services.graph_doc import validate_doc
from apps.artifacts.services.graph_runtime import check_graph_artifact
from apps.artifacts.services.query_context import resolve_artifact_queries
from apps.semantic.services.date_context import DateContextError

CONTEXT = {"as_of": "2026-09-16T13:00:00Z", "timezone": "America/New_York"}


def story(*, compare=False):
    return {
        "schema_version": 1,
        "blocks": [
            {
                "id": "range",
                "type": "period_selector" if compare else "date_filter",
                "config": {"default_range" if compare else "default": "last_30_days"},
            },
            {
                "id": "q",
                "type": "semantic_query",
                "inputs": {
                    "compare" if compare else "date_range": {
                        "$ref": "range.pair" if compare else "range.value"
                    }
                },
                "config": {
                    "compare": compare,
                    "queries": {
                        "sessions": {
                            "measures": ["sessions.count"],
                            "time_dimension": "sessions.created_at",
                        }
                    },
                },
            },
        ],
    }


def test_artifact_default_and_changed_controls_resolve_same_query_shape():
    queries, context = resolve_artifact_queries(story(), CONTEXT)
    assert queries[0]["filters"][0]["values"] == ["2026-08-18", "2026-09-16"]
    updated, _ = resolve_artifact_queries(
        story(), {**CONTEXT, "sources": {"range": {"start": "2026-09-10", "end": "2026-09-16"}}}
    )
    assert updated[0]["filters"][0]["values"] == ["2026-09-10", "2026-09-16"]
    assert updated[0]["query_context"] == context


def test_inspector_and_checks_include_both_comparison_queries():
    queries, _ = resolve_artifact_queries(story(compare=True), CONTEXT)
    assert [q["name"] for q in queries] == ["q.sessions", "q.sessions_previous"]
    assert queries[0]["filters"][0]["values"] == ["2026-08-18", "2026-09-16"]
    assert queries[1]["filters"][0]["values"] == ["2026-07-19", "2026-08-17"]


def test_comparison_output_name_collisions_are_rejected():
    doc = story(compare=True)
    doc["blocks"][1]["config"]["queries"]["sessions_previous"] = {
        "measures": ["sessions.count"],
        "time_dimension": "sessions.created_at",
    }
    with pytest.raises(DateContextError, match="names collide"):
        resolve_artifact_queries(doc, CONTEXT)


def test_previous_year_control_change_applies_to_both_queries():
    doc = story(compare=True)
    doc["blocks"][0]["config"]["default_comparison"] = "previous_year"
    queries, _ = resolve_artifact_queries(
        doc, {**CONTEXT, "sources": {"range": {"start": "2026-09-10", "end": "2026-09-16"}}}
    )
    assert queries[0]["filters"][0]["values"] == ["2026-09-10", "2026-09-16"]
    assert queries[1]["filters"][0]["values"] == ["2025-09-10", "2025-09-16"]


def test_unknown_controls_and_missing_bindings_do_not_run_unfiltered():
    with pytest.raises(DateContextError):
        resolve_artifact_queries(story(), {**CONTEXT, "sources": {"injected": {}}})
    doc = story()
    doc["blocks"][1]["inputs"]["date_range"] = {"$ref": "missing.value"}
    with pytest.raises(DateContextError, match="Unresolved"):
        resolve_artifact_queries(doc, CONTEXT)


@pytest.mark.parametrize("compare", [False, True])
def test_explicit_unresolved_control_never_uses_its_saved_default(compare):
    with pytest.raises(DateContextError, match="date_range must be"):
        resolve_artifact_queries(story(compare=compare), {**CONTEXT, "sources": {"range": None}})


def test_comparison_cannot_silently_ignore_a_second_date_binding():
    doc = story(compare=True)
    doc["blocks"][1]["inputs"]["date_range"] = {
        "value": {"start": "2026-01-01", "end": "2026-01-31"}
    }
    assert "ambiguous_date_bindings" in {d["code"] for d in validate_doc(doc)}
    with pytest.raises(DateContextError, match="cannot also bind date_range"):
        resolve_artifact_queries(doc, CONTEXT)


def test_date_refs_use_the_same_block_id_contract_as_graph_validation():
    doc = story()
    doc["blocks"][0]["id"] = "range.nested"
    doc["blocks"][1]["inputs"]["date_range"] = {"$ref": "range.nested.value"}
    with pytest.raises(DateContextError, match="Unresolved"):
        resolve_artifact_queries(doc, CONTEXT)


@pytest.mark.parametrize("operator", [[], {}, None, 4])
def test_malformed_filter_operator_is_a_graph_diagnostic(operator):
    doc = story()
    doc["blocks"][1]["config"]["queries"]["sessions"]["filters"] = [
        {"field": "sessions.created_at", "operator": operator, "values": ["2026-01-01"]}
    ]
    assert "date_filter_value" in {d["code"] for d in validate_doc(doc)}


@pytest.mark.parametrize("raw_key", ["dateRange", "timeDimensions", "name"])
def test_rejected_query_keys_cannot_be_executed_by_the_inspector(raw_key):
    doc = story()
    doc["blocks"][1]["config"]["queries"]["sessions"][raw_key] = "not a semantic query key"
    with pytest.raises(DateContextError, match="Invalid artifact query"):
        resolve_artifact_queries(doc, CONTEXT)


@pytest.mark.asyncio
async def test_runtime_never_reports_success_for_a_truncated_comparison_pair(monkeypatch):
    doc = story(compare=True)
    doc["blocks"][1]["config"]["queries"] = {
        f"query_{index}": {"measures": ["sessions.count"], "time_dimension": "sessions.created_at"}
        for index in range(13)
    }
    run = AsyncMock()
    monkeypatch.setattr("apps.artifacts.services.graph_runtime.run_semantic_query", run)
    result = await check_graph_artifact(SimpleNamespace(data={"story_doc": doc}))
    assert result["success"] is False
    assert "query_check_limit" in {d["code"] for d in result["diagnostics"]}
    assert result["queries"] == []
    run.assert_not_awaited()


@pytest.mark.parametrize(
    "block", [None, {}, {"id": "broken", "type": "date_filter", "config": "invalid"}]
)
def test_malformed_saved_blocks_fail_validation(block):
    doc = story()
    doc["blocks"].append(block)
    with pytest.raises(DateContextError, match="Invalid artifact block"):
        resolve_artifact_queries(doc, CONTEXT)


@pytest.mark.parametrize("field", ["config", "inputs"])
@pytest.mark.parametrize("value", [[], False, 0, ""])
def test_falsy_malformed_block_fields_fail_closed(field, value):
    doc = story()
    doc["blocks"][1][field] = value
    with pytest.raises(DateContextError, match="Invalid artifact block"):
        resolve_artifact_queries(doc, CONTEXT)


@pytest.mark.parametrize("binding", [None, {"value": None}, {"value": {}, "$ref": "range.value"}])
def test_malformed_date_binding_is_not_an_all_time_query(binding):
    doc = story()
    doc["blocks"][1]["inputs"]["date_range"] = binding
    with pytest.raises(DateContextError):
        resolve_artifact_queries(doc, CONTEXT)


def test_literal_date_binding_requires_a_time_dimension_at_write_time():
    doc = story()
    doc["blocks"][1]["inputs"]["date_range"] = {
        "value": {"start": "2026-01-01", "end": "2026-03-31"}
    }
    doc["blocks"][1]["config"]["queries"]["sessions"].pop("time_dimension")
    assert any(d["code"] == "query_window_without_time_dimension" for d in validate_doc(doc))


def test_unused_invalid_date_control_does_not_block_unrelated_inspector_queries():
    doc = story()
    doc["blocks"].append(
        {"id": "unused", "type": "date_filter", "config": {"default": "last_60_days"}}
    )
    expected, _ = resolve_artifact_queries(story(), CONTEXT)
    actual, _ = resolve_artifact_queries(doc, CONTEXT)
    assert actual == expected
    doc["blocks"][1]["inputs"]["date_range"] = {"$ref": "unused.value"}
    with pytest.raises(DateContextError, match="Unsupported date preset"):
        resolve_artifact_queries(doc, CONTEXT)


def test_validation_rejects_unknown_preset_and_raw_preset_filter():
    doc = story()
    doc["blocks"][0]["config"]["default"] = "last_31_days"
    doc["blocks"][1]["config"]["queries"]["sessions"]["filters"] = [
        {"field": "sessions.created_at", "operator": "inDateRange", "values": ["last_90_days"]}
    ]
    assert {d["code"] for d in validate_doc(doc)} >= {"date_preset", "date_filter_value"}


@pytest.mark.asyncio
async def test_runtime_date_errors_preserve_document_diagnostics():
    doc = story()
    doc["blocks"][0]["config"]["default"] = "unsupported"
    result = await check_graph_artifact(SimpleNamespace(data={"story_doc": doc}))
    assert result["success"] is False
    assert {d["code"] for d in result["diagnostics"]} >= {"date_context", "date_preset"}
    assert result["manifest"]["entry_count"] == 1
    assert result["query_context"] is None


@pytest.mark.asyncio
async def test_comparison_checks_previous_period_keys_independently(monkeypatch):
    monkeypatch.setattr(
        "apps.artifacts.services.graph_runtime.Workspace.objects.aget",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        "apps.artifacts.services.graph_runtime.run_semantic_query",
        AsyncMock(
            side_effect=[
                {"columns": ["sessions.count"], "rows": [[18]], "row_count": 1},
                {"columns": ["unrelated.count"], "rows": [[10]], "row_count": 1},
            ]
        ),
    )
    artifact = SimpleNamespace(workspace_id="workspace", data={"story_doc": story(compare=True)})
    result = await check_graph_artifact(artifact)
    assert result["success"] is False
    assert len(result["key_warnings"]) == 1
    assert result["key_warnings"][0]["query_key"] == "q.sessions_previous"
    assert result["key_warnings"][0]["expected_key"] == "sessions_count"
