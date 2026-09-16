from datetime import UTC, datetime

import pytest

from apps.artifacts.services.graph_doc import validate_doc
from apps.artifacts.services.query_context import resolve_artifact_queries
from apps.semantic.services.date_context import (
    DateContextError,
    agent_date_context,
    comparison_range,
    date_context,
    query_context,
    resolve_date_range,
    resolve_query_dates,
    validate_date_filter,
)

CONTEXT = {"as_of": "2026-09-16T13:00:00Z", "timezone": "America/New_York"}


@pytest.mark.parametrize(
    ("preset", "start", "end"),
    [
        ("last_7_days", "2026-09-10", "2026-09-16"),
        ("last_30_days", "2026-08-18", "2026-09-16"),
        ("last_90_days", "2026-06-19", "2026-09-16"),
        ("today", "2026-09-16", "2026-09-16"),
        ("yesterday", "2026-09-15", "2026-09-15"),
        ("month_to_date", "2026-09-01", "2026-09-16"),
    ],
)
def test_inclusive_presets(preset, start, end):
    assert resolve_date_range({"preset": preset}, query_context(CONTEXT)) == {
        "start": start,
        "end": end,
    }


@pytest.mark.parametrize(
    ("zone", "today"), [("Asia/Singapore", "2026-09-16"), ("America/New_York", "2026-09-15")]
)
def test_same_instant_uses_explicit_reporting_timezone(zone, today):
    context = date_context({"as_of": "2026-09-16T00:30:00Z", "timezone": zone})
    assert context["today"] == today
    assert context["presets"]["today"]["start"] == today


@pytest.mark.parametrize("instant", ["2026-03-09T12:00:00Z", "2026-11-02T12:00:00Z"])
def test_dst_uses_calendar_days_not_elapsed_hours(instant):
    context = date_context({**CONTEXT, "as_of": instant})
    current = context["presets"]["last_7_days"]
    previous = current["comparisons"]["previous_period"]
    from datetime import date

    assert (date.fromisoformat(current["end"]) - date.fromisoformat(current["start"])).days == 6
    assert (date.fromisoformat(current["start"]) - date.fromisoformat(previous["end"])).days == 1
    assert (date.fromisoformat(previous["end"]) - date.fromisoformat(previous["start"])).days == 6


def test_previous_year_clamps_leap_day():
    assert comparison_range(
        {"start": "2024-02-29", "end": "2024-03-02"}, "previous_year", query_context(CONTEXT)
    ) == {
        "start": "2023-02-28",
        "end": "2023-03-02",
    }


@pytest.mark.parametrize(
    "value",
    [
        None,
        "last_30_days",
        {},
        {"preset": "last_31_days"},
        {"start": "2026-02-30", "end": "2026-03-01"},
        {"start": "2026-09-17", "end": "2026-09-16"},
        {"start": "2026-01-01"},
    ],
)
def test_bad_ranges_fail_closed(value):
    with pytest.raises(DateContextError):
        resolve_date_range(value, query_context(CONTEXT))


@pytest.mark.parametrize(
    "value",
    [
        {"timezone": "not/a/zone"},
        {"as_of": "2026-09-16"},
        {"as_of": 123},
        {"as_of": 0},
        {"as_of": ""},
        [],
        "bad",
    ],
)
def test_bad_context(value):
    with pytest.raises(DateContextError):
        query_context(value)


@pytest.mark.parametrize("comparison", ["previous_year", "previous_period"])
def test_comparison_outside_calendar_fails_validation(comparison):
    with pytest.raises(DateContextError, match="supported calendar"):
        comparison_range(
            {"start": "0001-01-01", "end": "0001-01-02"}, comparison, query_context(CONTEXT)
        )


@pytest.mark.parametrize(
    "values",
    [["last_90_days"], ["2026-02-30", "2026-03-02"], ["2026-09-16", "2026-09-15"], [None], []],
)
def test_invalid_cube_date_values_are_rejected(values):
    with pytest.raises(DateContextError):
        validate_date_filter({"operator": "inDateRange", "values": values})


def test_chat_range_compiles_to_explicit_dates_and_timezone():
    query = resolve_query_dates(
        {
            "measures": ["sessions.count"],
            "time_dimension": "sessions.created_at",
            "date_range": {"preset": "last_30_days"},
        },
        CONTEXT,
    )
    assert query["filters"] == [
        {
            "field": "sessions.created_at",
            "operator": "inDateRange",
            "values": ["2026-08-18", "2026-09-16"],
        }
    ]
    assert query["query_context"]["timezone"] == "America/New_York"


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


@pytest.mark.parametrize(
    "block", [None, {}, {"id": "broken", "type": "date_filter", "config": "invalid"}]
)
def test_malformed_saved_blocks_fail_validation(block):
    doc = story()
    doc["blocks"].append(block)
    with pytest.raises(DateContextError, match="Invalid artifact block"):
        resolve_artifact_queries(doc, CONTEXT)


@pytest.mark.asyncio
async def test_chat_tool_forwards_date_intent(monkeypatch):
    from unittest.mock import AsyncMock

    from mcp_server import server

    workspace = object()
    # This is an argument-forwarding unit test, not a DB lifecycle test. Earlier
    # integration tests can leave a connection in the shared async worker thread.
    monkeypatch.setattr("mcp_server.envelope._aclose_old_connections", AsyncMock())
    monkeypatch.setattr(server, "_resolve_accessible_workspace", AsyncMock(return_value=workspace))
    run = AsyncMock(return_value={"columns": [], "rows": [], "row_count": 0})
    monkeypatch.setattr(server, "run_semantic_query", run)
    result = await server.semantic_query(
        workspace_id="workspace",
        user_id="user",
        measures=["sessions.count"],
        time_dimension="sessions.created_at",
        date_range={"preset": "last_30_days"},
        query_context=CONTEXT,
    )
    assert result["success"] is True
    assert run.await_args.args[0] is workspace
    assert run.await_args.args[1]["date_range"] == {"preset": "last_30_days"}
    assert run.await_args.args[1]["query_context"] == CONTEXT
    assert run.await_args.kwargs["user_id"] == "user"


def test_validation_rejects_unknown_preset_and_raw_preset_filter():
    doc = story()
    doc["blocks"][0]["config"]["default"] = "last_31_days"
    doc["blocks"][1]["config"]["queries"]["sessions"]["filters"] = [
        {"field": "sessions.created_at", "operator": "inDateRange", "values": ["last_90_days"]}
    ]
    assert {d["code"] for d in validate_doc(doc)} >= {"date_preset", "date_filter_value"}


def test_agent_clock_is_runtime_not_model_memory(monkeypatch):
    monkeypatch.setattr("django.utils.timezone.now", lambda: datetime(2026, 9, 16, 12, tzinfo=UTC))
    assert "2026-09-16" in agent_date_context()
    assert "latest available data date" in agent_date_context()
