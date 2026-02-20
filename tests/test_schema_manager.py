from unittest.mock import MagicMock, patch

import pytest

from apps.projects.models import TenantSchema
from apps.projects.services.schema_manager import SchemaManager


@pytest.mark.django_db
class TestSchemaManager:
    def test_provision_creates_schema(self, tenant_membership):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with (
            patch(
                "apps.projects.services.schema_manager.get_managed_db_connection",
                return_value=mock_conn,
            ),
            patch(
                "apps.projects.services.schema_manager.psycopg2.extensions.quote_ident",
                side_effect=lambda name, _: f'"{name}"',
            ),
        ):
            mgr = SchemaManager()
            ts = mgr.provision(tenant_membership)

        assert ts.schema_name == mgr._sanitize_schema_name(tenant_membership.tenant_id)
        assert ts.state == "active"
        assert TenantSchema.objects.count() == 1
        # Verify DDL was executed
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("CREATE SCHEMA" in c for c in calls)

    def test_provision_returns_existing(self, tenant_membership):
        TenantSchema.objects.create(
            tenant_membership=tenant_membership,
            schema_name=tenant_membership.tenant_id,
            state="active",
        )

        with patch("apps.projects.services.schema_manager.get_managed_db_connection"):
            mgr = SchemaManager()
            mgr.provision(tenant_membership)

        assert TenantSchema.objects.count() == 1  # no duplicate
