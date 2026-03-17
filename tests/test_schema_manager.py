from unittest.mock import MagicMock, patch

import pytest

from apps.workspaces.models import TenantSchema
from apps.workspaces.services.schema_manager import SchemaManager, readonly_role_name


@pytest.mark.django_db
class TestSchemaManager:
    def test_provision_creates_schema(self, tenant_membership):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            mgr = SchemaManager()
            ts = mgr.provision(tenant_membership.tenant)

        assert ts.schema_name == mgr._sanitize_schema_name(tenant_membership.tenant.external_id)
        assert ts.state == "active"
        assert TenantSchema.objects.count() == 1
        # Verify DDL was executed
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("CREATE SCHEMA" in c for c in calls)

    def test_provision_returns_existing(self, tenant_membership):
        mgr = SchemaManager()
        schema_name = mgr._sanitize_schema_name(tenant_membership.tenant.external_id)
        TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name=schema_name,
            state="active",
        )

        # No DB connection should be needed when an active schema is found
        ts = mgr.provision(tenant_membership.tenant)

        assert TenantSchema.objects.count() == 1  # no duplicate
        assert ts.schema_name == schema_name


@pytest.mark.django_db
class TestSchemaManagerRoleCreation:
    def test_provision_creates_readonly_role(self, tenant_membership):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = None  # role doesn't exist yet

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            mgr = SchemaManager()
            ts = mgr.provision(tenant_membership.tenant)

        role_name = readonly_role_name(ts.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("CREATE ROLE" in c and role_name in c for c in calls), (
            f"Expected CREATE ROLE for {role_name} in DDL calls"
        )
        assert any("GRANT USAGE ON SCHEMA" in c for c in calls)
        assert any("ALTER DEFAULT PRIVILEGES" in c for c in calls)

    def test_create_physical_schema_creates_readonly_role(self, tenant_membership):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = None  # role doesn't exist yet

        from apps.workspaces.models import TenantSchema

        ts = TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="test_domain_r1a2b3c4",
            state="provisioning",
        )

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            mgr = SchemaManager()
            mgr.create_physical_schema(ts)

        role_name = readonly_role_name(ts.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("CREATE ROLE" in c and role_name in c for c in calls)


@pytest.mark.django_db
class TestSchemaManagerRoleTeardown:
    def test_teardown_drops_readonly_role(self, tenant_membership):
        from apps.workspaces.models import TenantSchema

        ts = TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="test_domain",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            mgr = SchemaManager()
            mgr.teardown(ts)

        role_name = readonly_role_name(ts.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("DROP ROLE IF EXISTS" in c and role_name in c for c in calls)

    def test_teardown_view_schema_drops_readonly_role(self, workspace):
        from apps.workspaces.models import WorkspaceViewSchema

        vs = WorkspaceViewSchema.objects.create(
            workspace=workspace,
            schema_name="ws_abc1234def56789",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            mgr = SchemaManager()
            mgr.teardown_view_schema(vs)

        role_name = readonly_role_name(vs.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("DROP ROLE IF EXISTS" in c and role_name in c for c in calls)


class TestReadonlyRoleName:
    def test_basic(self):
        assert readonly_role_name("tenant_abc123") == "tenant_abc123_ro"

    def test_view_schema(self):
        assert readonly_role_name("ws_abc1234def56789") == "ws_abc1234def56789_ro"

    def test_refresh_schema(self):
        assert readonly_role_name("test_domain_r1a2b3c4") == "test_domain_r1a2b3c4_ro"
