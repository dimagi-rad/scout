from unittest.mock import MagicMock, patch

import psycopg
import pytest
from django.core.management import call_command

from apps.workspaces.models import TenantSchema, WorkspaceViewSchema
from apps.workspaces.services.schema_manager import readonly_role_name


@pytest.mark.django_db
class TestBackfillReadonlyRoles:
    def test_backfills_active_tenant_schemas(self, tenant_membership):
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

    def test_skips_materializing_state(self, tenant_membership):
        """The dead MATERIALIZING state must not be selected (11#8)."""
        TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="mat_domain",
            state="materializing",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            call_command("backfill_readonly_roles")

        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert not any("CREATE ROLE" in c for c in calls)

    def test_drift_does_not_abort_remaining_schemas(self, tenant_membership):
        """One drifted schema (missing physical schema) must not strand the rest (11#8)."""
        TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="aaa_drifted",
            state="active",
        )
        good = TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="zzz_good",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None

        def execute_side_effect(query, *args, **kwargs):
            text = str(query)
            if "GRANT USAGE ON SCHEMA" in text and "aaa_drifted" in text:
                raise psycopg.errors.InvalidSchemaName("schema does not exist")

        mock_cursor.execute.side_effect = execute_side_effect
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            call_command("backfill_readonly_roles")

        good_role = readonly_role_name(good.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        # The good schema must still have been processed despite the earlier drift.
        assert any("CREATE ROLE" in c and good_role in c for c in calls)

    def test_dry_run_makes_no_changes(self, tenant_membership):
        """--dry-run must not issue any CREATE/GRANT statements (11#8)."""
        TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="dry_domain",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            call_command("backfill_readonly_roles", "--dry-run")

        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert not any("CREATE ROLE" in c for c in calls)
        assert not any("GRANT" in c for c in calls)

    def test_view_schema_backfill_grants_nothing_on_tenant_schemas(
        self, tenant_membership, workspace
    ):
        """The view role must NOT be granted any access to raw tenant schemas: the
        views run with owner privileges, so such grants are unnecessary cross-tenant
        over-exposure (and their default-ACL entries block role teardown)."""
        ts = TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="ws_tenant",
            state="active",
        )
        # Attach the tenant to the workspace so the view branch finds it.
        workspace.tenants.add(tenant_membership.tenant)
        vs = WorkspaceViewSchema.objects.create(
            workspace=workspace,
            schema_name="ws_view",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            call_command("backfill_readonly_roles")

        view_role = readonly_role_name(vs.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert not any(
            "GRANT" in c and ts.schema_name in c and view_role in c for c in calls
        ), "view role must not be granted access to raw tenant schemas"
        assert not any(
            "ALTER DEFAULT PRIVILEGES" in c and ts.schema_name in c and view_role in c
            for c in calls
        )
