from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.projects.models import MaterializationRun, TenantMetadata, TenantSchema, TenantWorkspace
from apps.users.models import TenantMembership, User


@pytest.fixture
def user(db):
    return User.objects.create_user(email="dev@example.com", password="pw")


@pytest.fixture
def membership(user):
    return TenantMembership.objects.create(
        user=user,
        provider="commcare",
        tenant_id="test-domain",
        tenant_name="Test Domain",
    )


@pytest.fixture
def tenant_schema(membership):
    return TenantSchema.objects.create(
        tenant_membership=membership,
        schema_name="test_domain",
        state="active",
    )


@pytest.fixture
def materialization_run(tenant_schema):
    return MaterializationRun.objects.create(
        tenant_schema=tenant_schema,
        pipeline="commcare",
        state="completed",
    )


@pytest.fixture
def tenant_metadata(membership):
    return TenantMetadata.objects.create(
        tenant_membership=membership,
        metadata={"cases": 42},
    )


@pytest.fixture
def workspace():
    return TenantWorkspace.objects.create(
        tenant_id="test-domain",
        tenant_name="Test Domain",
        data_dictionary={"tables": []},
    )


@pytest.mark.django_db
def test_purge_requires_confirm_flag():
    """Without --confirm the command exits without deleting anything."""
    with pytest.raises(SystemExit) as exc_info:
        call_command("purge_synced_data")
    assert exc_info.value.code == 0


@pytest.mark.django_db
def test_purge_dry_run_preserves_data(tenant_schema, tenant_metadata, workspace):
    """Dry run does not delete any records."""
    with pytest.raises(SystemExit):
        call_command("purge_synced_data")

    assert TenantSchema.objects.count() == 1
    assert TenantMetadata.objects.count() == 1
    workspace.refresh_from_db()
    assert workspace.data_dictionary is not None


@pytest.mark.django_db
def test_purge_deletes_tenant_schemas(tenant_schema, materialization_run):
    """--confirm deletes TenantSchema and cascades to MaterializationRun."""
    with patch(
        "apps.projects.management.commands.purge_synced_data.SchemaManager.teardown"
    ) as mock_teardown:
        call_command("purge_synced_data", confirm=True)

    mock_teardown.assert_called_once()
    assert TenantSchema.objects.count() == 0
    assert MaterializationRun.objects.count() == 0


@pytest.mark.django_db
def test_purge_deletes_tenant_metadata(tenant_metadata):
    """--confirm deletes TenantMetadata records."""
    with patch("apps.projects.management.commands.purge_synced_data.SchemaManager.teardown"):
        call_command("purge_synced_data", confirm=True)

    assert TenantMetadata.objects.count() == 0


@pytest.mark.django_db
def test_purge_clears_data_dictionary(workspace):
    """--confirm clears data_dictionary on TenantWorkspace without deleting the workspace."""
    with patch("apps.projects.management.commands.purge_synced_data.SchemaManager.teardown"):
        call_command("purge_synced_data", confirm=True)

    workspace.refresh_from_db()
    assert workspace.data_dictionary is None
    assert workspace.data_dictionary_generated_at is None
    assert TenantWorkspace.objects.count() == 1  # workspace preserved


@pytest.mark.django_db
def test_purge_continues_on_schema_teardown_error(tenant_schema, tenant_metadata):
    """Schema teardown errors are logged but records are still deleted."""
    with patch(
        "apps.projects.management.commands.purge_synced_data.SchemaManager.teardown",
        side_effect=Exception("connection refused"),
    ):
        call_command("purge_synced_data", confirm=True)

    assert TenantSchema.objects.count() == 0
    assert TenantMetadata.objects.count() == 0
