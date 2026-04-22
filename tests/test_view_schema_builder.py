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
        provider="commcare", external_id="build-domain-a", canonical_name="domain_a"
    )
    t2 = Tenant.objects.create(
        provider="commcare", external_id="build-domain-b", canonical_name="domain_b"
    )
    ws = Workspace.objects.create(name="Build WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=t1)
    WorkspaceTenant.objects.create(workspace=ws, tenant=t2)
    return ws, t1, t2


def test_build_view_schema_creates_namespaced_views(two_tenant_workspace, managed_db_connection):
    """Two tenants with the same table produce separate namespaced views — no UNION ALL."""
    from apps.workspaces.models import TenantSchema, WorkspaceViewSchema
    from apps.workspaces.services.schema_manager import SchemaManager

    ws, t1, t2 = two_tenant_workspace

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

        c2 = conn.cursor()
        try:
            # Namespaced views must exist — no merged UNION ALL view
            c2.execute(f"SELECT id, name FROM {vs.schema_name}.domain_a__cases ORDER BY id")
            rows_a = c2.fetchall()
            c2.execute(f"SELECT id, name FROM {vs.schema_name}.domain_b__cases ORDER BY id")
            rows_b = c2.fetchall()
        finally:
            c2.close()

        assert len(rows_a) == 1
        assert rows_a[0][0] == "1"
        assert len(rows_b) == 1
        assert rows_b[0][0] == "2"
    finally:
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


def test_build_view_schema_no_union_all_no_tenant_column(
    two_tenant_workspace, managed_db_connection
):
    """Views are simple SELECT * aliases — no _tenant discriminator column."""
    from apps.workspaces.models import TenantSchema
    from apps.workspaces.services.schema_manager import SchemaManager

    ws, t1, t2 = two_tenant_workspace

    ts1 = TenantSchema.objects.create(
        tenant=t1, schema_name="build_domain_a_no_union", state=SchemaState.ACTIVE
    )
    ts2 = TenantSchema.objects.create(
        tenant=t2, schema_name="build_domain_b_no_union", state=SchemaState.ACTIVE
    )
    conn = managed_db_connection
    c = conn.cursor()
    try:
        c.execute("CREATE SCHEMA IF NOT EXISTS build_domain_a_no_union")
        c.execute("CREATE TABLE IF NOT EXISTS build_domain_a_no_union.forms (id TEXT, data TEXT)")
        c.execute("CREATE SCHEMA IF NOT EXISTS build_domain_b_no_union")
        c.execute("CREATE TABLE IF NOT EXISTS build_domain_b_no_union.forms (id TEXT, data TEXT)")
    finally:
        c.close()

    vs = None
    try:
        vs = SchemaManager().build_view_schema(ws)

        c2 = conn.cursor()
        try:
            # Column list for domain_a__forms should NOT include _tenant
            c2.execute(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_schema = '{vs.schema_name}' AND table_name = 'domain_a__forms'"
            )
            cols = {row[0] for row in c2.fetchall()}
        finally:
            c2.close()

        assert "_tenant" not in cols
        assert "id" in cols
        assert "data" in cols
    finally:
        c3 = conn.cursor()
        try:
            if vs:
                c3.execute(f"DROP SCHEMA IF EXISTS {vs.schema_name} CASCADE")
            c3.execute("DROP SCHEMA IF EXISTS build_domain_a_no_union CASCADE")
            c3.execute("DROP SCHEMA IF EXISTS build_domain_b_no_union CASCADE")
        finally:
            c3.close()
        if vs:
            vs.delete()
        ts1.delete()
        ts2.delete()


def test_build_view_schema_uses_canonical_name_not_external_id(
    two_tenant_workspace, managed_db_connection
):
    """View names use slugified canonical_name, not external_id."""
    from apps.workspaces.models import TenantSchema
    from apps.workspaces.services.schema_manager import SchemaManager

    ws, t1, t2 = two_tenant_workspace

    # t1 has canonical_name="domain_a", external_id="build-domain-a"
    # The prefix should be derived from "domain_a" → "domain_a"
    ts1 = TenantSchema.objects.create(
        tenant=t1, schema_name="build_domain_a_canon", state=SchemaState.ACTIVE
    )
    ts2 = TenantSchema.objects.create(
        tenant=t2, schema_name="build_domain_b_canon", state=SchemaState.ACTIVE
    )
    conn = managed_db_connection
    c = conn.cursor()
    try:
        c.execute("CREATE SCHEMA IF NOT EXISTS build_domain_a_canon")
        c.execute("CREATE TABLE IF NOT EXISTS build_domain_a_canon.visits (id TEXT)")
        c.execute("CREATE SCHEMA IF NOT EXISTS build_domain_b_canon")
    finally:
        c.close()

    vs = None
    try:
        vs = SchemaManager().build_view_schema(ws)

        c2 = conn.cursor()
        try:
            c2.execute(
                "SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = '{vs.schema_name}'"
            )
            view_names = {row[0] for row in c2.fetchall()}
        finally:
            c2.close()

        # canonical_name-based prefix
        assert "domain_a__visits" in view_names
        # external_id-based prefix should NOT appear
        assert "build_domain_a__visits" not in view_names
        assert "build_domain_b__visits" not in view_names
    finally:
        c3 = conn.cursor()
        try:
            if vs:
                c3.execute(f"DROP SCHEMA IF EXISTS {vs.schema_name} CASCADE")
            c3.execute("DROP SCHEMA IF EXISTS build_domain_a_canon CASCADE")
            c3.execute("DROP SCHEMA IF EXISTS build_domain_b_canon CASCADE")
        finally:
            c3.close()
        if vs:
            vs.delete()
        ts1.delete()
        ts2.delete()


def test_build_view_schema_three_tables_three_views(two_tenant_workspace, managed_db_connection):
    """A tenant with 3 tables produces exactly 3 namespaced views."""
    from apps.workspaces.models import TenantSchema
    from apps.workspaces.services.schema_manager import SchemaManager

    ws, t1, t2 = two_tenant_workspace

    ts1 = TenantSchema.objects.create(
        tenant=t1, schema_name="build_domain_a_three", state=SchemaState.ACTIVE
    )
    ts2 = TenantSchema.objects.create(
        tenant=t2, schema_name="build_domain_b_three", state=SchemaState.ACTIVE
    )
    conn = managed_db_connection
    c = conn.cursor()
    try:
        c.execute("CREATE SCHEMA IF NOT EXISTS build_domain_a_three")
        c.execute("CREATE TABLE IF NOT EXISTS build_domain_a_three.cases (id TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS build_domain_a_three.forms (id TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS build_domain_a_three.visits (id TEXT)")
        c.execute("CREATE SCHEMA IF NOT EXISTS build_domain_b_three")
    finally:
        c.close()

    vs = None
    try:
        vs = SchemaManager().build_view_schema(ws)

        c2 = conn.cursor()
        try:
            c2.execute(
                "SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = '{vs.schema_name}'"
            )
            view_names = {row[0] for row in c2.fetchall()}
        finally:
            c2.close()

        assert "domain_a__cases" in view_names
        assert "domain_a__forms" in view_names
        assert "domain_a__visits" in view_names
        # t2 has no tables so no views for domain_b
        domain_b_views = {v for v in view_names if v.startswith("domain_b__")}
        assert len(domain_b_views) == 0
    finally:
        c3 = conn.cursor()
        try:
            if vs:
                c3.execute(f"DROP SCHEMA IF EXISTS {vs.schema_name} CASCADE")
            c3.execute("DROP SCHEMA IF EXISTS build_domain_a_three CASCADE")
            c3.execute("DROP SCHEMA IF EXISTS build_domain_b_three CASCADE")
        finally:
            c3.close()
        if vs:
            vs.delete()
        ts1.delete()
        ts2.delete()


def test_build_view_schema_readonly_role_has_access(two_tenant_workspace, managed_db_connection):
    """Read-only role has access to the view schema and both underlying tenant schemas."""
    from apps.workspaces.models import TenantSchema
    from apps.workspaces.services.schema_manager import SchemaManager, readonly_role_name

    ws, t1, t2 = two_tenant_workspace

    ts1 = TenantSchema.objects.create(
        tenant=t1, schema_name="build_domain_a_ro", state=SchemaState.ACTIVE
    )
    ts2 = TenantSchema.objects.create(
        tenant=t2, schema_name="build_domain_b_ro", state=SchemaState.ACTIVE
    )
    conn = managed_db_connection
    c = conn.cursor()
    try:
        c.execute("CREATE SCHEMA IF NOT EXISTS build_domain_a_ro")
        c.execute("CREATE TABLE IF NOT EXISTS build_domain_a_ro.cases (id TEXT)")
        c.execute("CREATE SCHEMA IF NOT EXISTS build_domain_b_ro")
        c.execute("CREATE TABLE IF NOT EXISTS build_domain_b_ro.cases (id TEXT)")
    finally:
        c.close()

    vs = None
    try:
        vs = SchemaManager().build_view_schema(ws)
        view_role = readonly_role_name(vs.schema_name)

        c2 = conn.cursor()
        try:
            # View schema USAGE grant
            c2.execute(
                "SELECT has_schema_privilege(%s, %s, 'USAGE')",
                (view_role, vs.schema_name),
            )
            assert c2.fetchone()[0] is True

            # Tenant schema USAGE grants
            for schema in ("build_domain_a_ro", "build_domain_b_ro"):
                c2.execute(
                    "SELECT has_schema_privilege(%s, %s, 'USAGE')",
                    (view_role, schema),
                )
                assert c2.fetchone()[0] is True
        finally:
            c2.close()
    finally:
        c3 = conn.cursor()
        try:
            if vs:
                c3.execute(f"DROP SCHEMA IF EXISTS {vs.schema_name} CASCADE")
            c3.execute("DROP SCHEMA IF EXISTS build_domain_a_ro CASCADE")
            c3.execute("DROP SCHEMA IF EXISTS build_domain_b_ro CASCADE")
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
        with (
            CaptureQueriesContext(connection) as ctx,
            patch(
                "apps.workspaces.services.schema_manager.get_managed_db_connection"
            ) as mock_conn_fn,
        ):
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
