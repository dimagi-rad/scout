"""Direct tests for the refresh_tenant_schema task."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.workspaces.models import SchemaState, TenantSchema


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


def _mock_registry(provider="commcare"):
    """Return a mock registry whose list() yields one pipeline for the given provider."""
    pipeline = MagicMock()
    pipeline.provider = provider
    pipeline.name = f"{provider}_sync"
    registry = MagicMock()
    registry.list.return_value = [pipeline]
    registry.get.return_value = MagicMock()
    return registry


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_marks_schema_active_on_success(
    provisioning_schema, tenant_membership_obj
):
    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch(
            "apps.workspaces.tasks.resolve_credential",
            return_value={"type": "api_key", "value": "tok"},
        ),
        patch(
            "apps.workspaces.tasks.get_registry",
            return_value=_mock_registry(),
        ),
        patch("apps.workspaces.tasks.run_pipeline"),
    ):
        from apps.workspaces.tasks import refresh_tenant_schema

        result = await refresh_tenant_schema(
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
        )

    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.ACTIVE
    assert result["status"] == "active"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_schedules_old_schema_teardown(
    provisioning_schema, old_active_schema, tenant_membership_obj
):
    """Old ACTIVE schemas are moved to TEARDOWN and a delayed teardown is scheduled."""
    deferrer = MagicMock()
    deferrer.defer_async = AsyncMock(return_value=1)

    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch(
            "apps.workspaces.tasks.resolve_credential",
            return_value={"type": "api_key", "value": "tok"},
        ),
        patch(
            "apps.workspaces.tasks.get_registry",
            return_value=_mock_registry(),
        ),
        patch("apps.workspaces.tasks.run_pipeline"),
        patch(
            "apps.workspaces.tasks.teardown_schema.configure",
            return_value=deferrer,
        ) as mock_configure,
    ):
        from apps.workspaces.tasks import refresh_tenant_schema

        await refresh_tenant_schema(
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
        )

    await old_active_schema.arefresh_from_db()
    assert old_active_schema.state == SchemaState.TEARDOWN
    mock_configure.assert_called_once_with(schedule_in={"seconds": 30 * 60})
    deferrer.defer_async.assert_awaited_once_with(schema_id=str(old_active_schema.id))


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_marks_failed_on_schema_creation_error(
    provisioning_schema, tenant_membership_obj
):
    with patch(
        "apps.workspaces.services.schema_manager.get_managed_db_connection",
        side_effect=RuntimeError("Managed DB unreachable"),
    ):
        from apps.workspaces.tasks import refresh_tenant_schema

        result = await refresh_tenant_schema(
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
        )

    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.FAILED
    assert "error" in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_marks_failed_on_no_credential(
    provisioning_schema, tenant_membership_obj
):
    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch("apps.workspaces.tasks.resolve_credential", return_value=None),
        patch("apps.workspaces.services.schema_manager.SchemaManager.teardown"),
    ):
        from apps.workspaces.tasks import refresh_tenant_schema

        result = await refresh_tenant_schema(
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
        )

    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.FAILED
    assert "error" in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_marks_failed_on_materialization_error(
    provisioning_schema, tenant_membership_obj
):
    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch(
            "apps.workspaces.tasks.resolve_credential",
            return_value={"type": "api_key", "value": "tok"},
        ),
        patch(
            "apps.workspaces.tasks.get_registry",
            return_value=_mock_registry(),
        ),
        patch(
            "apps.workspaces.tasks.run_pipeline",
            side_effect=RuntimeError("Pipeline exploded"),
        ),
        patch("apps.workspaces.services.schema_manager.SchemaManager.teardown"),
    ):
        from apps.workspaces.tasks import refresh_tenant_schema

        result = await refresh_tenant_schema(
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
        )

    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.FAILED
    assert "error" in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_returns_error_for_unknown_schema(tenant_membership_obj):
    from apps.workspaces.tasks import refresh_tenant_schema

    result = await refresh_tenant_schema(
        schema_id="00000000-0000-0000-0000-000000000000",
        membership_id=str(tenant_membership_obj.id),
    )
    assert "error" in result
