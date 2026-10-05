"""Deterministic calendar dates shared by chat, artifact queries, and checks.

Dates are inclusive calendar dates in an explicit IANA timezone, not elapsed
24-hour periods. A render/check captures one as_of instant for every query.
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone

PRESETS = ("last_7_days", "last_30_days", "last_90_days", "month_to_date", "today", "yesterday")
COMPARISONS = ("previous_period", "previous_year")
DEFAULT_PRESET = "last_30_days"
DEFAULT_COMPARISON = "previous_period"
PERIOD_UNITS = {"day", "week", "month", "quarter", "year"}


class DateContextError(ValueError):
    """Invalid date intent; callers must not run an unfiltered substitute."""


def query_context(value=None) -> dict:
    if value is not None and not isinstance(value, dict):
        raise DateContextError("query_context must be an object.")
    value = value or {}
    zone_name = value.get("timezone", settings.TIME_ZONE)
    try:
        zone = ZoneInfo(zone_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise DateContextError("Use a valid IANA timezone in query_context.timezone.") from exc
    if "as_of" in value:
        try:
            now = datetime.fromisoformat(value["as_of"].replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise DateContextError(
                "query_context.as_of must be an ISO timestamp with a timezone."
            ) from exc
        if now.tzinfo is None:
            raise DateContextError("query_context.as_of must be an ISO timestamp with a timezone.")
    else:
        now = timezone.now()
    try:
        return {
            "as_of": now.isoformat(),
            "timezone": zone.key,
            "today": now.astimezone(zone).date().isoformat(),
        }
    except (OverflowError, ValueError) as exc:
        raise DateContextError("Date context is outside the supported calendar range.") from exc


def calendar_date(value) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise DateContextError("Use calendar dates in YYYY-MM-DD format.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DateContextError(f"Invalid calendar date: {value}.") from exc


def resolve_date_range(value, context: dict) -> dict:
    if not isinstance(value, dict):
        raise DateContextError(
            "date_range must be {preset: ...}, {last: N, unit: day|week|month|quarter|year}, "
            "or {start: YYYY-MM-DD, end: YYYY-MM-DD}."
        )
    # Explicit bounds are already resolved, including custom and comparison
    # windows. A preset annotation must never override these visible dates.
    if "last" in value or "unit" in value:
        validate_period_range(value)
        end = calendar_date(context.get("today") if isinstance(context, dict) else None)
        start = _period_start(end, value["last"], value["unit"])
    elif "start" in value or "end" in value:
        start, end = calendar_date(value.get("start")), calendar_date(value.get("end"))
    else:
        preset = value.get("preset")
        if preset not in PRESETS:
            raise DateContextError(f"Unsupported date preset. Use one of: {', '.join(PRESETS)}.")
        end = calendar_date(context.get("today") if isinstance(context, dict) else None)
        start = end
        try:
            if preset == "yesterday":
                start = end = end - timedelta(days=1)
            elif preset == "month_to_date":
                start = end.replace(day=1)
            elif preset.startswith("last_"):
                start = end - timedelta(days=int(preset.split("_")[1]) - 1)
        except OverflowError as exc:
            raise DateContextError("Date range is outside the supported calendar range.") from exc
    if start > end:
        raise DateContextError("Date range start must be on or before its end.")
    return {"start": start.isoformat(), "end": end.isoformat()}


def validate_period_range(value: dict) -> None:
    count, unit = value.get("last"), value.get("unit")
    if (
        set(value) != {"last", "unit"}
        or type(count) is not int
        or count < 1
        or not isinstance(unit, str)
        or unit not in PERIOD_UNITS
    ):
        raise DateContextError(
            "Counted date_range must be {last: positive integer, unit: day|week|month|quarter|year}. "
            "Do not mix it with presets or explicit bounds."
        )


def _period_start(end: date, count: int, unit: str) -> date:
    """Include the current calendar bucket through today; Cube weeks start Monday."""
    try:
        if unit == "day":
            return end - timedelta(days=count - 1)
        if unit == "week":
            return end - timedelta(days=end.weekday() + 7 * (count - 1))
        width = {"month": 1, "quarter": 3, "year": 12}[unit]
        month_index = (end.year - 1) * 12 + ((end.month - 1) // width) * width
        month_index -= width * (count - 1)
        return date(month_index // 12 + 1, month_index % 12 + 1, 1)
    except (OverflowError, ValueError) as exc:
        raise DateContextError("Date range is outside the supported calendar range.") from exc


def comparison_range(value, comparison: str, context: dict) -> dict:
    current = resolve_date_range(value, context)
    start, end = calendar_date(current["start"]), calendar_date(current["end"])
    if comparison == "previous_period":
        width = (end - start).days + 1
        if start.toordinal() <= width:
            raise DateContextError("Comparison is outside the supported calendar range.")
        start, end = start - timedelta(days=width), end - timedelta(days=width)
    elif comparison == "previous_year":
        if start.year == 1:
            raise DateContextError("Comparison is outside the supported calendar range.")

        def previous_year(day):
            return day.replace(
                year=day.year - 1, day=min(day.day, calendar.monthrange(day.year - 1, day.month)[1])
            )

        start, end = previous_year(start), previous_year(end)
    else:
        raise DateContextError("Unsupported comparison. Use previous_period or previous_year.")
    return {"start": start.isoformat(), "end": end.isoformat()}


def date_context(value=None) -> dict:
    context = query_context(value)
    context["presets"] = {
        preset: {
            **resolve_date_range({"preset": preset}, context),
            "preset": preset,
            "comparisons": {
                comparison: comparison_range({"preset": preset}, comparison, context)
                for comparison in COMPARISONS
            },
        }
        for preset in PRESETS
    }
    return context


def resolve_query_dates(query: dict, context=None) -> dict:
    result = dict(query)
    supplied = context if context is not None else result.get("query_context")
    if "date_range" not in result and supplied is None:
        return result
    resolved_context = query_context(supplied)
    result["query_context"] = resolved_context
    if "date_range" in result:
        member = result.get("time_dimension") or result.get("timeDimension")
        if not member:
            raise DateContextError("A date_range requires time_dimension.")
        bounds = resolve_date_range(result.pop("date_range"), resolved_context)
        existing = result.get("filters") or []
        existing = existing if isinstance(existing, list) else [existing]
        result["filters"] = [
            *existing,
            {
                "field": member,
                "operator": "inDateRange",
                "values": [bounds["start"], bounds["end"]],
            },
        ]
    return result


def validate_date_filter(spec: dict, timezone_name: str | None = None) -> None:
    operator = spec.get("operator", "equals")
    if not isinstance(operator, str):
        raise DateContextError("Filter operator must be a string.")
    if operator not in {
        "inDateRange",
        "notInDateRange",
        "beforeDate",
        "beforeOrOnDate",
        "afterDate",
        "afterOrOnDate",
    }:
        return
    values = spec.get("value") if "value" in spec else spec.get("values")
    values = values if isinstance(values, list) else [values]
    allowed_lengths = {1, 2} if operator in {"inDateRange", "notInDateRange"} else {1}
    if len(values) not in allowed_lengths:
        raise DateContextError(f"Invalid number of dates for {operator}.")
    parsed = []
    try:
        zone = ZoneInfo(timezone_name or settings.TIME_ZONE)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise DateContextError("Use a valid IANA timezone for date filters.") from exc
    for index, value in enumerate(values):
        try:
            if not isinstance(value, str):
                raise TypeError
            if "T" in value or " " in value:
                instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            else:
                instant = datetime.combine(
                    calendar_date(value), time.max if index == 1 else time.min
                )
            parsed.append(
                (instant if instant.tzinfo else instant.replace(tzinfo=zone)).astimezone(UTC)
            )
        except (OverflowError, TypeError, ValueError) as exc:
            raise DateContextError(
                f"Unsupported date filter value {value!r}. Use ISO calendar dates or timestamps within the supported calendar range. For relative dates use date_range={{preset: last_30_days}}."
            ) from exc
    if len(parsed) == 2 and parsed[0] > parsed[1]:
        raise DateContextError("Date range start must be on or before its end.")


def agent_date_context() -> str:
    context = query_context()
    return (
        "\n## Current date context\n"
        f"Reporting timezone: {context['timezone']}. Today: {context['today']}.\n"
        "This is the current clock, not the latest available data date. Use supported date presets "
        "and let the runtime resolve them. Never infer today from model memory or source timestamps.\n"
    )
