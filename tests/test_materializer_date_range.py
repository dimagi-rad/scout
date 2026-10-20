"""The load summary's per-table date range (#917)."""

from unittest import mock

import psycopg
import pytest
from django.db import connection

from mcp_server.services import materializer
from mcp_server.services.materializer import _source_date_range


@pytest.fixture
def scratch_table(db):
    params = connection.get_connection_params()
    conninfo = psycopg.conninfo.make_conninfo(
        dbname=params["dbname"],
        user=params.get("user"),
        password=params.get("password"),
        host=params.get("host"),
        port=params.get("port"),
    )

    def connect():
        return psycopg.connect(conninfo, autocommit=True)

    with connect() as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS date_range_t")
        conn.execute("DROP TABLE IF EXISTS date_range_t.raw_forms")
        conn.execute("CREATE TABLE date_range_t.raw_forms (received_on text)")
        conn.execute("DROP TABLE IF EXISTS date_range_t.raw_visits")
        conn.execute("CREATE TABLE date_range_t.raw_visits (visit_date timestamptz)")
    with mock.patch.object(materializer, "get_managed_db_connection", side_effect=connect):
        yield connect
    with connect() as conn:
        conn.execute("DROP SCHEMA date_range_t CASCADE")


def test_reports_the_earliest_and_latest_event_time(scratch_table):
    with scratch_table() as conn:
        conn.execute(
            "INSERT INTO date_range_t.raw_forms VALUES "
            "('2025-03-01T10:00:00Z'), ('2024-01-15T08:30:00'), ('not a date'), (NULL)"
        )

    result = _source_date_range("date_range_t", "commcare", "raw_forms")

    assert result == {
        "column": "received_on",
        "earliest": "2024-01-15T08:30:00+00:00",
        "latest": "2025-03-01T10:00:00+00:00",
    }


def test_reads_a_native_timestamp_column_directly(scratch_table):
    with scratch_table() as conn:
        conn.execute(
            "INSERT INTO date_range_t.raw_visits VALUES "
            "('2025-06-01T00:00:00Z'), ('2026-02-03T12:00:00Z'), (NULL)"
        )

    result = _source_date_range("date_range_t", "commcare_connect", "raw_visits")

    assert result == {
        "column": "visit_date",
        "earliest": "2025-06-01T00:00:00+00:00",
        "latest": "2026-02-03T12:00:00+00:00",
    }


def test_no_dated_rows_gives_no_range(scratch_table):
    with scratch_table() as conn:
        conn.execute("INSERT INTO date_range_t.raw_forms VALUES ('not a date')")

    assert _source_date_range("date_range_t", "commcare", "raw_forms") is None


def test_a_table_without_a_declared_event_time_is_skipped(scratch_table):
    assert _source_date_range("date_range_t", "commcare", "raw_users") is None


def test_a_failing_query_does_not_fail_the_load(scratch_table):
    assert _source_date_range("missing_schema", "commcare", "raw_forms") is None
