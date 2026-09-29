"""Provider rows without a natural key must neither fail a page nor collapse (#263, 02#7)."""

import logging
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from mcp_server.services.materializer import (
    _write_cases,
    _write_connect_users,
    _write_connect_visits,
    _write_forms,
    _write_ocs_experiments,
    _write_ocs_participants,
    _write_ocs_sessions,
)

WRITERS = [
    pytest.param(_write_cases, "raw_cases", "case_id", ["c1", "c2"], "", id="cases"),
    pytest.param(_write_forms, "raw_forms", "form_id", ["f1", "f2"], "", id="forms"),
    pytest.param(_write_connect_visits, "raw_visits", "visit_id", [1, 2], None, id="visits"),
    pytest.param(_write_connect_users, "raw_users", "username", ["u1", "u2"], "", id="users"),
    pytest.param(
        _write_ocs_experiments, "raw_experiments", "experiment_id", ["e1", "e2"], "", id="exps"
    ),
    pytest.param(_write_ocs_sessions, "raw_sessions", "session_id", ["s1", "s2"], "", id="sess"),
    pytest.param(
        _write_ocs_participants,
        "raw_participants",
        "participant_id",
        ["p1", "p2"],
        "",
        id="participants",
    ),
]


@pytest.fixture
def managed_conn():
    url = os.environ.get("MANAGED_DATABASE_URL")
    if not url:
        pytest.skip("MANAGED_DATABASE_URL not set")
    schema = f"test_natural_keys_{uuid4().hex}"
    conn = psycopg.connect(url, autocommit=True)
    conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    conn.autocommit = False
    try:
        yield conn, schema
    finally:
        conn.rollback()
        conn.autocommit = True
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        conn.close()


@pytest.mark.parametrize(("writer", "table", "key", "ids", "missing"), WRITERS)
@pytest.mark.parametrize("absent", [True, False], ids=["key-absent", "key-empty"])
def test_keyless_rows_are_skipped_and_logged(
    managed_conn, caplog, writer, table, key, ids, missing, absent
):
    conn, schema = managed_conn
    keyless = {} if absent else {key: missing}
    page = [{key: ids[0]}, dict(keyless), {key: ids[1]}, dict(keyless)]

    with caplog.at_level(logging.WARNING, logger="mcp_server.services.materializer"):
        written = writer(iter([(page, len(page))]), schema, conn)

    rows = conn.execute(
        sql.SQL("SELECT {} FROM {} ORDER BY 1").format(
            sql.Identifier(key), sql.Identifier(schema, table)
        )
    ).fetchall()
    assert [r[0] for r in rows] == ids
    assert written == 2
    assert f"Skipped 2 {table} rows with no {key}" in caplog.text
