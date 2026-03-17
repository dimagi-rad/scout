from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from apps.workspaces.services.schema_manager import readonly_role_name


@pytest.mark.django_db
class TestBackfillReadonlyRoles:
    def test_backfills_active_tenant_schemas(self, tenant_membership):
        from apps.workspaces.models import TenantSchema

        ts = TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="test_domain",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None  # role doesn't exist yet
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            call_command("backfill_readonly_roles")

        role_name = readonly_role_name(ts.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("CREATE ROLE" in c and role_name in c for c in calls)
        assert any("GRANT USAGE ON SCHEMA" in c for c in calls)
        assert any("ALTER DEFAULT PRIVILEGES" in c for c in calls)
        # Should also grant SELECT ON ALL TABLES for existing tables
        assert any("GRANT SELECT ON ALL TABLES" in c for c in calls)

    def test_skips_teardown_schemas(self, tenant_membership):
        from apps.workspaces.models import TenantSchema

        TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="old_domain",
            state="teardown",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            call_command("backfill_readonly_roles")

        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert not any("CREATE ROLE" in c for c in calls)

    def test_idempotent_existing_role(self, tenant_membership):
        from apps.workspaces.models import TenantSchema

        TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="test_domain",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (1,)  # role already exists
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            call_command("backfill_readonly_roles")

        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        # Should NOT create the role (already exists)
        assert not any("CREATE ROLE" in c for c in calls)
        # But should still grant (idempotent grants are safe)
        assert any("GRANT USAGE ON SCHEMA" in c for c in calls)
