from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import pytest

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
from mcp_server import server

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


@pytest.mark.asyncio
async def test_chat_tool_forwards_date_intent(monkeypatch):
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
    assert run.await_args.args[1]["query_context"] == {"timezone": CONTEXT["timezone"]}
    assert run.await_args.kwargs["user_id"] == "user"


@pytest.mark.asyncio
async def test_chat_replayed_context_resolves_presets_at_the_current_clock(monkeypatch):
    monkeypatch.setattr("mcp_server.envelope._aclose_old_connections", AsyncMock())
    monkeypatch.setattr(server, "_resolve_accessible_workspace", AsyncMock(return_value=object()))
    monkeypatch.setattr("django.utils.timezone.now", lambda: datetime(2026, 9, 18, 13, tzinfo=UTC))

    async def execute(workspace, query, **kwargs):
        return {
            "semantic_query": resolve_query_dates(query),
            "rows": [],
            "columns": [],
            "row_count": 0,
        }

    monkeypatch.setattr(server, "run_semantic_query", execute)
    result = await server.semantic_query(
        workspace_id="workspace",
        user_id="user",
        measures=["sessions.count"],
        time_dimension="sessions.created_at",
        date_range={"preset": "last_30_days"},
        query_context=CONTEXT,
    )
    assert result["data"]["semantic_query"]["filters"][0]["values"] == ["2026-08-20", "2026-09-18"]


def test_date_filters_preserve_space_separated_iso_timestamps():
    spec = {"operator": "inDateRange", "values": ["2026-01-01 10:00:00", "2026-01-02 10:00:00"]}
    validate_date_filter(spec)
    with pytest.raises(DateContextError, match="start must be on or before"):
        validate_date_filter({**spec, "values": list(reversed(spec["values"]))})


def test_agent_clock_is_runtime_not_model_memory(monkeypatch):
    monkeypatch.setattr("django.utils.timezone.now", lambda: datetime(2026, 9, 16, 12, tzinfo=UTC))
    assert "2026-09-16" in agent_date_context()
    assert "latest available data date" in agent_date_context()


def test_agent_clock_is_stable_within_reporting_day(monkeypatch):
    monkeypatch.setattr("django.utils.timezone.now", lambda: datetime(2026, 9, 16, 12, tzinfo=UTC))
    first = agent_date_context()
    monkeypatch.setattr("django.utils.timezone.now", lambda: datetime(2026, 9, 16, 13, tzinfo=UTC))
    assert agent_date_context() == first
    monkeypatch.setattr("django.utils.timezone.now", lambda: datetime(2026, 9, 17, 13, tzinfo=UTC))
    assert agent_date_context() != first


def test_scalar_filter_is_preserved_with_relative_dates():
    original = {"field": "sessions.status", "operator": "equals", "value": "complete"}
    query = resolve_query_dates(
        {
            "time_dimension": "sessions.created_at",
            "date_range": {"preset": "today"},
            "filters": original,
        },
        CONTEXT,
    )
    assert query["filters"][0] == original
    assert len(query["filters"]) == 2


@pytest.mark.parametrize("operator", [None, [], {}, 0])
def test_malformed_operator_is_validation_error(operator):
    with pytest.raises(DateContextError, match="operator must be a string"):
        validate_date_filter({"operator": operator})


@pytest.mark.parametrize("context", [None, [], {}, CONTEXT])
def test_unresolved_context_is_validation_error(context):
    with pytest.raises(DateContextError):
        resolve_date_range({"preset": "today"}, context)


@pytest.mark.parametrize(
    "bounds",
    [
        ["2026-09-16T00:00:00Z", "2026-09-15T00:00:00Z"],
        ["2026-09-16T01:00:00+00:00", "2026-09-16T02:00:00+02:00"],
        ["2026-09-16T12:00:00", "2026-09-16T11:00:00"],
    ],
)
def test_reversed_timestamp_filters_are_rejected(bounds):
    with pytest.raises(DateContextError, match="on or before"):
        validate_date_filter({"operator": "inDateRange", "values": bounds})


def test_timestamp_ordering_uses_offsets_and_inclusive_date_end():
    validate_date_filter(
        {"operator": "inDateRange", "values": ["2026-09-16T01:00:00+02:00", "2026-09-15T23:30:00Z"]}
    )
    validate_date_filter(
        {"operator": "inDateRange", "values": ["2026-09-16T20:00:00Z", "2026-09-16"]},
        "America/New_York",
    )


def test_timestamp_conversion_outside_calendar_is_validation_error():
    with pytest.raises(DateContextError):
        validate_date_filter({"operator": "afterDate", "values": ["0001-01-01T00:00:00+02:00"]})


def test_date_filter_invalid_timezone_is_validation_error():
    with pytest.raises(DateContextError, match="valid IANA timezone"):
        validate_date_filter({"operator": "afterDate", "values": ["2026-09-16"]}, "not/a/zone")
