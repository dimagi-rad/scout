import os

import pytest

from apps.projects.models import (
    SchemaState,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.users.models import Tenant

pytestmark = pytest.mark.skipif(
    not os.environ.get("MANAGED_DATABASE_URL"),
    reason="MANAGED_DATABASE_URL not set",
)


@pytest.fixture
def managed_db_connection():
    from apps.projects.services.schema_manager import get_managed_db_connection

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
    from apps.projects.models import TenantSchema, WorkspaceViewSchema
    from apps.projects.services.schema_manager import SchemaManager

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
