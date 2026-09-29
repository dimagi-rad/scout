"""0016 retires the never-written MATERIALIZING schema state (#251)."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceViewSchema

BEFORE = ("workspaces", "0015_tenant_load_generation")
AFTER = ("workspaces", "0016_drop_materializing_schema_state")


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


@pytest.fixture
def old_apps(transactional_db):
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        yield _migrate([BEFORE])
    finally:
        _migrate(leaves)


def test_stray_materializing_rows_are_expired_for_reverification(old_apps):
    tenant = old_apps.get_model("users", "Tenant").objects.create(
        provider="commcare", external_id="stray"
    )
    workspace = old_apps.get_model("workspaces", "Workspace").objects.create(name="Stray")
    old_tenant_schema = old_apps.get_model("workspaces", "TenantSchema")
    stray = old_tenant_schema.objects.create(
        tenant=tenant, schema_name="t_stray", state="materializing"
    )
    serving = old_tenant_schema.objects.create(
        tenant=tenant, schema_name="t_serving", state="active"
    )
    view = old_apps.get_model("workspaces", "WorkspaceViewSchema").objects.create(
        workspace=workspace, schema_name="ws_stray", state="materializing"
    )

    _migrate([AFTER])

    assert TenantSchema.objects.get(pk=stray.pk).state == SchemaState.EXPIRED
    assert TenantSchema.objects.get(pk=serving.pk).state == SchemaState.ACTIVE
    assert WorkspaceViewSchema.objects.get(pk=view.pk).state == SchemaState.EXPIRED
    assert "materializing" not in SchemaState.values
