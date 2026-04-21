"""Tests for OCS materializer writer functions."""

from __future__ import annotations

import os

import pytest
from psycopg import sql as psql

from apps.users.models import Tenant, TenantCredential, TenantMembership
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
    TenantCredential.objects.create(tenant_membership=tm, credential_type=TenantCredential.OAUTH)
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
                    [
                        {
                            "experiment_id": "exp-1",
                            "name": "Bot",
                            "url": "https://x",
                            "version_number": 1,
                        }
                    ]
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
                    ]
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
                    ]
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


def test_write_ocs_participants_creates_table_and_rows(tenant_schema):
    conn = get_managed_db_connection()
    conn.autocommit = False
    try:
        n = _write_ocs_participants(
            iter([[{"identifier": "p1", "platform": "web", "remote_id": "r1"}]]),
            tenant_schema.schema_name,
            conn,
        )
        conn.commit()
        assert n == 1
        assert _row_count(conn, tenant_schema.schema_name, "raw_participants") == 1
    finally:
        conn.close()
