"""Declared provider event times, not heuristics based on column names."""

import re
from datetime import UTC, datetime

SOURCE_TIME_COLUMNS = {
    "commcare": {
        "raw_forms": {"received_on", "server_modified_on"},
        "raw_cases": {
            "date_opened",
            "last_modified",
            "server_last_modified",
            "indexed_on",
            "date_closed",
        },
    },
    "ocs": {"raw_sessions": {"created_at", "updated_at"}, "raw_messages": {"created_at"}},
    "commcare_connect": {"raw_visits": {"visit_date"}},
}

# ISO calendar dates/times only: never let PostgreSQL interpret relative words
# ("now", "tomorrow") or locale-sensitive survey text as a stable event time.
ISO_EVENT_TIME = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"([T ]([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]([.][0-9]+)?"
    r"(Z|[+-](0[0-9]|1[0-4])(:[0-5][0-9])?)?)?$"
)
OFFSET_SUFFIX = r"[T ].*(Z|[+-][0-9]{2}(:[0-9]{2})?)$"


def normalize_event_time(value):
    """Invalid values become NULL; offset-less provider times are explicitly UTC."""
    if not isinstance(value, str) or re.fullmatch(ISO_EVENT_TIME, value) is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def event_time_sql(column: str) -> str:
    """Safe typed projection for pre-upgrade text snapshots (PostgreSQL 16+)."""
    identifier = '"' + column.replace('"', '""') + '"'
    value = f"{identifier}::text"
    return (
        f"CASE WHEN {value} ~ '{ISO_EVENT_TIME}' "
        f"AND pg_input_is_valid({value}, 'timestamp with time zone') "
        f"THEN CASE WHEN {value} ~ '{OFFSET_SUFFIX}' "
        f"THEN {value}::timestamptz ELSE {value}::timestamp AT TIME ZONE 'UTC' END END"
    )
