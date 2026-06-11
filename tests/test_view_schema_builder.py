import os
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model

from apps.users.models import Tenant
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.schema_manager import SchemaManager

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


# --- Truncation-safety and idempotency coverage -----------------------------

# Exact production tenant whose long canonical name + table names previously
# truncated two distinct views to the same 63-byte identifier.
_PIPN_NAME = "Kangaroo Mother Care- Preterm Infants Parents Network (PIPN)"


def _make_long_named_workspace(canonical_a, external_a, canonical_b, external_b):
    """Build a 2-tenant workspace with arbitrary canonical names / external ids."""

    User = get_user_model()
    user = User.objects.create_user(email="longnames@example.com", password="pass")
    t1 = Tenant.objects.create(
        provider="commcare", external_id=external_a, canonical_name=canonical_a
    )
    t2 = Tenant.objects.create(
        provider="commcare", external_id=external_b, canonical_name=canonical_b
    )
    ws = Workspace.objects.create(name="Long WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=t1)
    WorkspaceTenant.objects.create(workspace=ws, tenant=t2)
    return ws, t1, t2


def test_build_view_schema_long_canonical_name_no_truncation_collision(db, managed_db_connection):
    """The exact production tenant ("...PIPN") whose long name made
    raw_completed_works and raw_completed_modules truncate to the same 63-byte
    identifier now produces two distinct, <=63-byte views and the build succeeds."""
    ws, t1, _t2 = _make_long_named_workspace(_PIPN_NAME, "pipn-001", "Short Partner", "short-001")

    ts1 = TenantSchema.objects.create(
        tenant=t1, schema_name="build_pipn_long", state=SchemaState.ACTIVE
    )
    ts2 = TenantSchema.objects.create(
        tenant=_t2, schema_name="build_pipn_short", state=SchemaState.ACTIVE
    )
    conn = managed_db_connection
    c = conn.cursor()
    try:
        c.execute("CREATE SCHEMA IF NOT EXISTS build_pipn_long")
        # Two tables whose composed view names previously collided after truncation
        c.execute(
            "CREATE TABLE IF NOT EXISTS build_pipn_long.raw_completed_works "
            "(opportunity_id TEXT, payment_date TEXT)"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS build_pipn_long.raw_completed_modules "
            "(module TEXT, completed_at TEXT)"
        )
        c.execute("CREATE SCHEMA IF NOT EXISTS build_pipn_short")
    finally:
        c.close()

    vs = None
    try:
        # Must not raise InvalidTableDefinition / column-rename error
        vs = SchemaManager().build_view_schema(ws)
        assert vs.state == SchemaState.ACTIVE

        prefix = SchemaManager()._view_prefix(t1)
        works_view = f"{prefix}__raw_completed_works"
        modules_view = f"{prefix}__raw_completed_modules"

        # Distinct view names, both within PG's 63-byte identifier limit
        assert works_view != modules_view
        assert len(works_view.encode("utf-8")) <= 63
        assert len(modules_view.encode("utf-8")) <= 63

        c2 = conn.cursor()
        try:
            c2.execute(
                "SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = '{vs.schema_name}'"
            )
            view_names = {row[0] for row in c2.fetchall()}
            # Each view exposes its own source columns (no silent redefinition)
            c2.execute(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_schema = '{vs.schema_name}' AND table_name = %s",
                (works_view,),
            )
            works_cols = {row[0] for row in c2.fetchall()}
        finally:
            c2.close()

        assert works_view in view_names
        assert modules_view in view_names
        assert works_cols == {"opportunity_id", "payment_date"}
    finally:
        c3 = conn.cursor()
        try:
            if vs:
                c3.execute(f"DROP SCHEMA IF EXISTS {vs.schema_name} CASCADE")
            c3.execute("DROP SCHEMA IF EXISTS build_pipn_long CASCADE")
            c3.execute("DROP SCHEMA IF EXISTS build_pipn_short CASCADE")
        finally:
            c3.close()
        if vs:
            vs.delete()
        ts1.delete()
        ts2.delete()


def test_build_view_schema_two_long_names_shared_head_get_distinct_prefixes(
    db, managed_db_connection
):
    """Two long canonical names sharing their first 23 sanitized chars must still
    get distinct prefixes (disambiguated by the external_id digest) so their
    views do not collide."""
    shared_head = "Maternal Child Health Program "  # >23 chars once sanitized
    ws, t1, t2 = _make_long_named_workspace(
        shared_head + "Northern Region Implementation",
        "mch-north-1",
        shared_head + "Southern Region Implementation",
        "mch-south-1",
    )

    mgr = SchemaManager()
    p1 = mgr._view_prefix(t1)
    p2 = mgr._view_prefix(t2)
    # Both bounded and distinct despite identical 23-char heads
    assert len(p1) <= 32
    assert len(p2) <= 32
    assert p1[:23] == p2[:23]
    assert p1 != p2

    ts1 = TenantSchema.objects.create(
        tenant=t1, schema_name="build_mch_north", state=SchemaState.ACTIVE
    )
    ts2 = TenantSchema.objects.create(
        tenant=t2, schema_name="build_mch_south", state=SchemaState.ACTIVE
    )
    conn = managed_db_connection
    c = conn.cursor()
    try:
        c.execute("CREATE SCHEMA IF NOT EXISTS build_mch_north")
        c.execute("CREATE TABLE IF NOT EXISTS build_mch_north.cases (id TEXT)")
        c.execute("CREATE SCHEMA IF NOT EXISTS build_mch_south")
        c.execute("CREATE TABLE IF NOT EXISTS build_mch_south.cases (id TEXT)")
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
        assert f"{p1}__cases" in view_names
        assert f"{p2}__cases" in view_names
    finally:
        c3 = conn.cursor()
        try:
            if vs:
                c3.execute(f"DROP SCHEMA IF EXISTS {vs.schema_name} CASCADE")
            c3.execute("DROP SCHEMA IF EXISTS build_mch_north CASCADE")
            c3.execute("DROP SCHEMA IF EXISTS build_mch_south CASCADE")
        finally:
            c3.close()
        if vs:
            vs.delete()
        ts1.delete()
        ts2.delete()


def test_build_view_schema_idempotent_rebuild_after_column_change(
    two_tenant_workspace, managed_db_connection
):
    """A rebuild after an underlying table's columns were renamed must succeed.

    The workspace view from the first build survives into the second build. With
    the old ``CREATE OR REPLACE VIEW`` path this raised
    "cannot change name of view column ..." (the exact production error). The
    drop-schema-and-recreate rebuild makes it idempotent — the second build
    reflects the new column set."""
    ws, t1, _t2 = two_tenant_workspace

    ts1 = TenantSchema.objects.create(
        tenant=t1, schema_name="build_idem_a", state=SchemaState.ACTIVE
    )
    ts2 = TenantSchema.objects.create(
        tenant=_t2, schema_name="build_idem_b", state=SchemaState.ACTIVE
    )
    conn = managed_db_connection
    c = conn.cursor()
    try:
        c.execute("CREATE SCHEMA IF NOT EXISTS build_idem_a")
        c.execute("CREATE TABLE IF NOT EXISTS build_idem_a.reports (module TEXT, score TEXT)")
        c.execute("CREATE SCHEMA IF NOT EXISTS build_idem_b")
    finally:
        c.close()

    vs = None
    try:
        # First build — view has columns (module, score)
        vs = SchemaManager().build_view_schema(ws)
        c2 = conn.cursor()
        try:
            c2.execute(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_schema = '{vs.schema_name}' AND table_name = 'domain_a__reports'"
            )
            cols_before = {row[0] for row in c2.fetchall()}
        finally:
            c2.close()
        assert cols_before == {"module", "score"}

        # Rename the source columns in place. The first build's view SURVIVES
        # (ALTER does not cascade-drop it), so the second build must redefine a
        # view whose column names changed — the case CREATE OR REPLACE VIEW
        # rejected. Add a brand-new column too, to assert the new definition.
        c4 = conn.cursor()
        try:
            c4.execute("ALTER TABLE build_idem_a.reports RENAME COLUMN module TO opportunity_id")
            c4.execute("ALTER TABLE build_idem_a.reports RENAME COLUMN score TO payment_amount")
            c4.execute("ALTER TABLE build_idem_a.reports ADD COLUMN status TEXT")
        finally:
            c4.close()

        # Second build must succeed (no column-rename error) and reflect new cols
        vs2 = SchemaManager().build_view_schema(ws)
        assert vs2.state == SchemaState.ACTIVE

        c5 = conn.cursor()
        try:
            c5.execute(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_schema = '{vs.schema_name}' AND table_name = 'domain_a__reports'"
            )
            cols_after = {row[0] for row in c5.fetchall()}
        finally:
            c5.close()
        assert cols_after == {"opportunity_id", "payment_amount", "status"}
    finally:
        c6 = conn.cursor()
        try:
            if vs:
                c6.execute(f"DROP SCHEMA IF EXISTS {vs.schema_name} CASCADE")
            c6.execute("DROP SCHEMA IF EXISTS build_idem_a CASCADE")
            c6.execute("DROP SCHEMA IF EXISTS build_idem_b CASCADE")
        finally:
            c6.close()
        if vs:
            vs.delete()
        ts1.delete()
        ts2.delete()


def test_build_view_schema_oversized_view_name_raises(two_tenant_workspace, managed_db_connection):
    """A composed view name exceeding 63 bytes must raise ValueError naming the
    offending view BEFORE any DDL, rather than letting PostgreSQL silently
    truncate it."""
    ws, t1, _t2 = two_tenant_workspace

    ts1 = TenantSchema.objects.create(
        tenant=t1, schema_name="build_oversize_a", state=SchemaState.ACTIVE
    )
    ts2 = TenantSchema.objects.create(
        tenant=_t2, schema_name="build_oversize_b", state=SchemaState.ACTIVE
    )
    # t1 canonical_name "domain_a" -> prefix "domain_a" (8 chars) + "__" = 10.
    # A 60-char table name pushes the composed name to 70 bytes (>63).
    long_table = "x" * 60
    conn = managed_db_connection
    c = conn.cursor()
    try:
        c.execute("CREATE SCHEMA IF NOT EXISTS build_oversize_a")
        c.execute(f'CREATE TABLE IF NOT EXISTS build_oversize_a."{long_table}" (id TEXT)')
        c.execute("CREATE SCHEMA IF NOT EXISTS build_oversize_b")
    finally:
        c.close()

    try:
        with pytest.raises(ValueError, match="63-byte"):
            SchemaManager().build_view_schema(ws)
        # Offending view name is identified in the message
        prefix = SchemaManager()._view_prefix(t1)
        try:
            SchemaManager().build_view_schema(ws)
        except ValueError as exc:
            assert f"{prefix}__{long_table}" in str(exc)
    finally:
        c3 = conn.cursor()
        try:
            view_schema = SchemaManager()._view_schema_name(ws.id)
            c3.execute(f"DROP SCHEMA IF EXISTS {view_schema} CASCADE")
            c3.execute("DROP SCHEMA IF EXISTS build_oversize_a CASCADE")
            c3.execute("DROP SCHEMA IF EXISTS build_oversize_b CASCADE")
        finally:
            c3.close()
        WorkspaceViewSchema.objects.filter(workspace=ws).delete()
        ts1.delete()
        ts2.delete()

@pytest.mark.django_db
def test_build_view_schema_clears_last_error_on_success(workspace, tenant):
    """A successful build must clear any stale last_error from a prior failure."""
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value = mock_cursor

    ts = TenantSchema.objects.create(
        tenant=tenant, schema_name="test_domain_clear", state=SchemaState.ACTIVE
    )
    # Seed a pre-existing FAILED view schema with a stale error.
    WorkspaceViewSchema.objects.create(
        workspace=workspace,
        schema_name="ws_stale_error",
        state=SchemaState.FAILED,
        last_error="stale boom",
    )
    vs = None
    try:
        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            vs = SchemaManager().build_view_schema(workspace)
        assert vs.state == SchemaState.ACTIVE
        assert vs.last_error == ""
        vs.refresh_from_db()
        assert vs.last_error == ""
    finally:
        ts.delete()
        if vs:
            vs.delete()


@pytest.mark.django_db
def test_build_view_schema_records_last_error_on_failure(workspace, tenant):
    """On the FAILED path, the exception text is persisted to last_error."""
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_cursor.execute.side_effect = RuntimeError("relation does not exist")
    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value = mock_cursor

    ts = TenantSchema.objects.create(
        tenant=tenant, schema_name="test_domain_fail", state=SchemaState.ACTIVE
    )
    try:
        with (
            patch(
                "apps.workspaces.services.schema_manager.get_managed_db_connection",
                return_value=mock_conn,
            ),
            pytest.raises(RuntimeError),
        ):
            SchemaManager().build_view_schema(workspace)

        vs = WorkspaceViewSchema.objects.get(workspace=workspace)
        assert vs.state == SchemaState.FAILED
        assert "relation does not exist" in vs.last_error
    finally:
        ts.delete()
        WorkspaceViewSchema.objects.filter(workspace=workspace).delete()
