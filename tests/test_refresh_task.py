"""Direct tests for the refresh_tenant_schema Celery task."""

from unittest.mock import MagicMock, patch

import pytest

from apps.projects.models import SchemaState, TenantSchema


@pytest.fixture
def provisioning_schema(db, tenant):
    return TenantSchema.objects.create(
        tenant=tenant,
        schema_name="test_domain_r12345678",
        state=SchemaState.PROVISIONING,
    )


@pytest.fixture
def old_active_schema(db, tenant):
    return TenantSchema.objects.create(
        tenant=tenant,
        schema_name="test_domain",
        state=SchemaState.ACTIVE,
    )


@pytest.fixture
def tenant_membership_obj(db, user, tenant):
    from apps.users.models import TenantMembership

    tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
    return tm


def _mock_conn():
    conn = MagicMock()
    conn.cursor.return_value = MagicMock()
    return conn


@pytest.mark.django_db
def test_refresh_task_marks_schema_active_on_success(provisioning_schema, tenant_membership_obj):
    with (
        patch(
            "apps.projects.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch(
            "apps.projects.tasks._resolve_credential",
            return_value={"type": "api_key", "value": "tok"},
        ),
        patch("mcp_server.pipeline_registry.get_registry") as mock_registry,
        patch("mcp_server.services.materializer.run_pipeline"),
    ):
        mock_registry.return_value.get.return_value = MagicMock()
        from apps.projects.tasks import refresh_tenant_schema

        result = refresh_tenant_schema(str(provisioning_schema.id), str(tenant_membership_obj.id))

    provisioning_schema.refresh_from_db()
    assert provisioning_schema.state == SchemaState.ACTIVE
    assert result["status"] == "active"


@pytest.mark.django_db
def test_refresh_task_drops_old_active_schema_on_success(
    provisioning_schema, old_active_schema, tenant_membership_obj
):
    with (
        patch(
            "apps.projects.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch(
            "apps.projects.tasks._resolve_credential",
            return_value={"type": "api_key", "value": "tok"},
        ),
        patch("mcp_server.pipeline_registry.get_registry") as mock_registry,
        patch("mcp_server.services.materializer.run_pipeline"),
        patch("apps.projects.services.schema_manager.SchemaManager.teardown"),
    ):
        mock_registry.return_value.get.return_value = MagicMock()
        from apps.projects.tasks import refresh_tenant_schema

        refresh_tenant_schema(str(provisioning_schema.id), str(tenant_membership_obj.id))

    old_active_schema.refresh_from_db()
    assert old_active_schema.state == SchemaState.EXPIRED


@pytest.mark.django_db
def test_refresh_task_marks_failed_on_schema_creation_error(
    provisioning_schema, tenant_membership_obj
):
    with patch(
        "apps.projects.services.schema_manager.get_managed_db_connection",
        side_effect=RuntimeError("Managed DB unreachable"),
    ):
        from apps.projects.tasks import refresh_tenant_schema

        result = refresh_tenant_schema(str(provisioning_schema.id), str(tenant_membership_obj.id))

    provisioning_schema.refresh_from_db()
    assert provisioning_schema.state == SchemaState.FAILED
    assert "error" in result


@pytest.mark.django_db
def test_refresh_task_marks_failed_on_no_credential(provisioning_schema, tenant_membership_obj):
    with (
        patch(
            "apps.projects.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch("apps.projects.tasks._resolve_credential", return_value=None),
        patch("apps.projects.services.schema_manager.SchemaManager.teardown"),
    ):
        from apps.projects.tasks import refresh_tenant_schema

        result = refresh_tenant_schema(str(provisioning_schema.id), str(tenant_membership_obj.id))

    provisioning_schema.refresh_from_db()
    assert provisioning_schema.state == SchemaState.FAILED
    assert "error" in result


@pytest.mark.django_db
def test_refresh_task_marks_failed_on_materialization_error(
    provisioning_schema, tenant_membership_obj
):
    with (
        patch(
            "apps.projects.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch(
            "apps.projects.tasks._resolve_credential",
            return_value={"type": "api_key", "value": "tok"},
        ),
        patch("mcp_server.pipeline_registry.get_registry") as mock_registry,
        patch(
            "mcp_server.services.materializer.run_pipeline",
            side_effect=RuntimeError("Pipeline exploded"),
        ),
        patch("apps.projects.services.schema_manager.SchemaManager.teardown"),
    ):
        mock_registry.return_value.get.return_value = MagicMock()
        from apps.projects.tasks import refresh_tenant_schema

        result = refresh_tenant_schema(str(provisioning_schema.id), str(tenant_membership_obj.id))

    provisioning_schema.refresh_from_db()
    assert provisioning_schema.state == SchemaState.FAILED
    assert "error" in result


@pytest.mark.django_db
def test_refresh_task_returns_error_for_unknown_schema(tenant_membership_obj):
    from apps.projects.tasks import refresh_tenant_schema

    result = refresh_tenant_schema(
        "00000000-0000-0000-0000-000000000000", str(tenant_membership_obj.id)
    )
    assert "error" in result
