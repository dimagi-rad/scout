"""Tests for OCS materializer writer functions."""

from __future__ import annotations

import os

import pytest
from psycopg import sql as psql

from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.workspaces.services.schema_manager import (
    SchemaManager,
    get_managed_db_connection,
)
from mcp_server.services.materializer import (
    _write_ocs_experiments,
    _write_ocs_messages,
    _write_ocs_participants,
    _write_ocs_sessions,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("MANAGED_DATABASE_URL"),
    reason="MANAGED_DATABASE_URL not set",
)


@pytest.fixture
def tenant_schema(db, user):
    tenant = Tenant.objects.create(
        provider="ocs", external_id="exp-uuid-1", canonical_name="Test Bot"
    )
    tm = TenantMembership.objects.create(user=user, tenant=tenant)
    conn, _ = TenantConnection.objects.get_or_create(
        user=tm.user,
        provider=tm.tenant.provider,
        credential_type=TenantConnection.OAUTH,
    )
    tm.connection = conn
    tm.save(update_fields=["connection"])
    schema = SchemaManager().provision(tenant)
    yield schema
    conn = get_managed_db_connection()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            psql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psql.Identifier(schema.schema_name))
        )
    conn.close()


def _row_count(conn, schema, table):
    with conn.cursor() as cur:
        cur.execute(
            psql.SQL("SELECT count(*) FROM {}.{}").format(
                psql.Identifier(schema), psql.Identifier(table)
            )
        )
        return cur.fetchone()[0]


def test_write_ocs_experiments_creates_table_and_rows(tenant_schema):
    conn = get_managed_db_connection()
    conn.autocommit = False
    try:
        n = _write_ocs_experiments(
            iter(
                [
                    (
                        [
                            {
                                "experiment_id": "exp-1",
                                "name": "Bot",
                                "url": "https://x",
                                "version_number": 1,
                            }
                        ],
                        1,
                    )
                ]
            ),
            tenant_schema.schema_name,
            conn,
        )
        conn.commit()
        assert n == 1
        assert _row_count(conn, tenant_schema.schema_name, "raw_experiments") == 1
    finally:
        conn.close()


def test_write_ocs_sessions_creates_table_and_rows(tenant_schema):
    conn = get_managed_db_connection()
    conn.autocommit = False
    try:
        n = _write_ocs_sessions(
            iter(
                [
                    (
                        [
                            {
                                "session_id": "s1",
                                "experiment_id": "exp-1",
                                "participant_identifier": "p1",
                                "participant_platform": "web",
                                "created_at": "2026-04-01T00:00:00Z",
                                "updated_at": "2026-04-01T01:00:00Z",
                                "tags": ["a"],
                            }
                        ],
                        1,
                    )
                ]
            ),
            tenant_schema.schema_name,
            conn,
        )
        conn.commit()
        assert n == 1
        assert _row_count(conn, tenant_schema.schema_name, "raw_sessions") == 1
    finally:
        conn.close()


def test_write_ocs_messages_creates_table_and_rows(tenant_schema):
    conn = get_managed_db_connection()
    conn.autocommit = False
    try:
        n = _write_ocs_messages(
            iter(
                [
                    (
                        [
                            {
                                "message_id": "s1:0",
                                "session_id": "s1",
                                "message_index": 0,
                                "role": "user",
                                "content": "hi",
                                "created_at": "2026-04-01T00:00:00Z",
                                "metadata": {"k": "v"},
                                "tags": [],
                            }
                        ],
                        None,
                    )
                ]
            ),
            tenant_schema.schema_name,
            conn,
        )
        conn.commit()
        assert n == 1
        assert _row_count(conn, tenant_schema.schema_name, "raw_messages") == 1
    finally:
        conn.close()


def test_write_ocs_messages_reports_session_progress(tenant_schema):
    """The messages writer reports progress in sessions (one yielded tuple
    per session, empty or not) against the loader's session-count total,
    while still returning the number of message rows written."""
    conn = get_managed_db_connection()
    conn.autocommit = False
    progress: list[tuple[int, int | None]] = []

    def on_page(loaded: int, total: int | None) -> None:
        progress.append((loaded, total))

    def msg(session_id: str, idx: int) -> dict:
        return {
            "message_id": f"{session_id}:{idx}",
            "session_id": session_id,
            "message_index": idx,
            "role": "user",
            "content": "hi",
            "created_at": "2026-04-01T00:00:00Z",
            "metadata": {},
            "tags": [],
        }

    try:
        n = _write_ocs_messages(
            iter(
                [
                    ([msg("s1", 0), msg("s1", 1)], 3),
                    ([], 3),  # session with no messages still advances the bar
                    ([msg("s3", 0)], 3),
                ]
            ),
            tenant_schema.schema_name,
            conn,
            on_page=on_page,
        )
        conn.commit()
        assert n == 3
        assert _row_count(conn, tenant_schema.schema_name, "raw_messages") == 3
        assert progress == [(1, 3), (2, 3), (3, 3)]
    finally:
        conn.close()


def test_write_ocs_participants_creates_table_and_rows(tenant_schema):
    conn = get_managed_db_connection()
    conn.autocommit = False
    try:
        n = _write_ocs_participants(
            iter(
                [
                    (
                        [
                            {
                                "participant_id": "11111111-1111-1111-1111-111111111111",
                                "identifier": "p1",
                                "name": "John",
                                "platform": "api",
                                "remote_id": "r1",
                                "data": [
                                    {
                                        "chatbot": "Support Bot",
                                        "chatbot_id": "exp-uuid-1",
                                        "data": {"timezone": "Africa/Johannesburg"},
                                    }
                                ],
                            }
                        ],
                        None,
                    )
                ]
            ),
            tenant_schema.schema_name,
            conn,
        )
        conn.commit()
        assert n == 1
        assert _row_count(conn, tenant_schema.schema_name, "raw_participants") == 1
        with conn.cursor() as cur:
            cur.execute(
                psql.SQL(
                    "SELECT participant_id, identifier, name, platform, remote_id, data "
                    "FROM {}.raw_participants"
                ).format(psql.Identifier(tenant_schema.schema_name))
            )
            row = cur.fetchone()
        assert row[0] == "11111111-1111-1111-1111-111111111111"
        assert row[1] == "p1"
        assert row[2] == "John"
        assert row[3] == "api"
        assert row[4] == "r1"
        assert row[5] == [
            {
                "chatbot": "Support Bot",
                "chatbot_id": "exp-uuid-1",
                "data": {"timezone": "Africa/Johannesburg"},
            }
        ]
    finally:
        conn.close()
