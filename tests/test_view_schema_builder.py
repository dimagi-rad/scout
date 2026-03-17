import os
from unittest.mock import MagicMock, patch

import pytest

from apps.users.models import Tenant
from apps.workspaces.models import (
    SchemaState,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("MANAGED_DATABASE_URL"),
    reason="MANAGED_DATABASE_URL not set",
)


@pytest.fixture
def managed_db_connection():
    from apps.workspaces.services.schema_manager import get_managed_db_connection

    conn = get_managed_db_connection()
    yield conn
    if not conn.closed:
        conn.close()


@pytest.fixture
def two_tenant_workspace(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(email="builder@example.com", password="pass")
    t1 = Tenant.objects.create(
        provider="commcare", external_id="build-domain-a", canonical_name="A"
    )
    t2 = Tenant.objects.create(
        provider="commcare", external_id="build-domain-b", canonical_name="B"
    )
    ws = Workspace.objects.create(name="Build WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=t1)
    WorkspaceTenant.objects.create(workspace=ws, tenant=t2)
    return ws, t1, t2


def test_build_view_schema_creates_record(two_tenant_workspace, managed_db_connection):
    from apps.workspaces.models import TenantSchema, WorkspaceViewSchema
    from apps.workspaces.services.schema_manager import SchemaManager

    ws, t1, t2 = two_tenant_workspace

    # Create physical tenant schemas with a test table
    ts1 = TenantSchema.objects.create(
        tenant=t1, schema_name="build_domain_a_test", state=SchemaState.ACTIVE
    )
    ts2 = TenantSchema.objects.create(
        tenant=t2, schema_name="build_domain_b_test", state=SchemaState.ACTIVE
    )
    conn = managed_db_connection
    c = conn.cursor()
    try:
        c.execute("CREATE SCHEMA IF NOT EXISTS build_domain_a_test")
        c.execute("CREATE TABLE IF NOT EXISTS build_domain_a_test.cases (id TEXT, name TEXT)")
        c.execute("INSERT INTO build_domain_a_test.cases VALUES ('1', 'Alice')")
        c.execute("CREATE SCHEMA IF NOT EXISTS build_domain_b_test")
        c.execute(
            "CREATE TABLE IF NOT EXISTS build_domain_b_test.cases (id TEXT, name TEXT, status TEXT)"
        )
        c.execute("INSERT INTO build_domain_b_test.cases VALUES ('2', 'Bob', 'active')")
    finally:
        c.close()

    vs = None
    try:
        vs = SchemaManager().build_view_schema(ws)

        assert vs is not None
        assert vs.schema_name.startswith("ws_")
        assert WorkspaceViewSchema.objects.filter(workspace=ws).exists()

        # Verify the view exists and unions both tenants
        c2 = conn.cursor()
        try:
            c2.execute(f"SELECT id, name, _tenant FROM {vs.schema_name}.cases ORDER BY id")
            rows = c2.fetchall()
        finally:
            c2.close()
        assert len(rows) == 2
        tenants_seen = {r[2] for r in rows}
        assert "build-domain-a" in tenants_seen
        assert "build-domain-b" in tenants_seen
    finally:
        # Cleanup
        c3 = conn.cursor()
        try:
            if vs:
                c3.execute(f"DROP SCHEMA IF EXISTS {vs.schema_name} CASCADE")
            c3.execute("DROP SCHEMA IF EXISTS build_domain_a_test CASCADE")
            c3.execute("DROP SCHEMA IF EXISTS build_domain_b_test CASCADE")
        finally:
            c3.close()
        if vs:
            vs.delete()
        ts1.delete()
        ts2.delete()


@pytest.mark.django_db
def test_build_view_schema_bulk_fetches_tenant_schemas(workspace, tenant):
    """TenantSchema resolution uses one query, not N queries."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.workspaces.models import TenantSchema
    from apps.workspaces.services.schema_manager import SchemaManager

    ts = TenantSchema.objects.create(
        tenant=tenant, schema_name="test_domain_bulk", state=SchemaState.ACTIVE
    )
    try:
        with CaptureQueriesContext(connection) as ctx:
            with patch(
                "apps.workspaces.services.schema_manager.get_managed_db_connection"
            ) as mock_conn_fn:
                mock_cursor = MagicMock()
                mock_cursor.fetchall.return_value = []
                mock_conn = MagicMock()
                mock_conn.closed = False
                mock_conn.cursor.return_value = mock_cursor
                mock_conn_fn.return_value = mock_conn
                try:
                    SchemaManager().build_view_schema(workspace)
                except Exception:
                    pass  # may raise if no DB — we only care about query count

        tenant_schema_queries = [
            q
            for q in ctx.captured_queries
            if "tenantschema" in q["sql"].lower() and "SELECT" in q["sql"].upper()
        ]
        # Should be at most 1 SELECT query for TenantSchemas, not one per tenant
        assert len(tenant_schema_queries) <= 1
    finally:
        ts.delete()


@pytest.mark.django_db
def test_build_view_schema_returns_active_record(workspace, tenant):
    """build_view_schema must return a record with state=ACTIVE — it owns the full lifecycle."""
    from apps.workspaces.services.schema_manager import SchemaManager

    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value = mock_cursor

    # tenant needs an active TenantSchema for the workspace to have something to build
    from apps.workspaces.models import TenantSchema

    ts = TenantSchema.objects.create(
        tenant=tenant, schema_name="test_domain_schema", state=SchemaState.ACTIVE
    )
    try:
        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            vs = SchemaManager().build_view_schema(workspace)
        assert vs.state == SchemaState.ACTIVE
    finally:
        ts.delete()
        if vs:
            vs.delete()
