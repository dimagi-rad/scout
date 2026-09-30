"""A workspace with nothing serving reports ``not_loaded`` unless a load is running (#249 05#4).

``provisioning`` used to stand for "not serving yet", so a multi-source workspace
that was never loaded, or a source whose load died mid-way, showed a permanent
"Loading data..." spinner. Every surface that reports the status is pinned here.
"""

import pytest
from django.test import Client

from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.tasks import materialize_workspace
from tests.tenant_access import grant_tenant_access
from tests.test_chat_first_load import queued_jobs  # noqa: F401 (registers the fixture)

LOADING = MaterializationRun.RunState.LOADING


@pytest.fixture
def second_tenant(db, user, workspace):
    other = Tenant.objects.create(
        provider="commcare", external_id="second-domain", canonical_name="Second"
    )
    WorkspaceTenant.objects.create(workspace=workspace, tenant=other)
    grant_tenant_access(user, other)
    return other


def _statuses(user, workspace):
    client = Client()
    client.force_login(user)
    listed = next(w for w in client.get("/api/workspaces/").json() if w["id"] == str(workspace.id))
    detail = client.get(f"/api/workspaces/{workspace.id}/").json()
    return listed["schema_status"], detail["schema_status"]


def _running_load(tenant, state=SchemaState.PROVISIONING):
    schema = TenantSchema.objects.create(
        tenant=tenant, schema_name=f"s_{tenant.external_id}".replace("-", "_"), state=state
    )
    MaterializationRun.objects.create(tenant_schema=schema, pipeline="commcare_sync", state=LOADING)
    return schema


@pytest.mark.django_db
def test_never_loaded_multi_source_workspace_is_not_loaded(user, workspace, second_tenant):
    assert _statuses(user, workspace) == ("not_loaded", "not_loaded")


@pytest.mark.django_db
def test_never_loaded_single_source_workspace_is_not_loaded(user, workspace):
    assert _statuses(user, workspace) == ("not_loaded", "not_loaded")


@pytest.mark.django_db
def test_a_stranded_provisioning_schema_is_not_a_running_load(user, workspace, tenant):
    """A PROVISIONING row whose load died has no active run, so nothing is loading."""
    TenantSchema.objects.create(
        tenant=tenant, schema_name="stranded", state=SchemaState.PROVISIONING
    )
    assert _statuses(user, workspace) == ("not_loaded", "not_loaded")


@pytest.mark.django_db
def test_a_building_view_row_without_a_load_is_not_loaded(user, workspace, second_tenant):
    WorkspaceViewSchema.objects.create(
        workspace=workspace, schema_name="ws_stranded", state=SchemaState.PROVISIONING
    )
    assert _statuses(user, workspace) == ("not_loaded", "not_loaded")


@pytest.mark.django_db
def test_a_running_first_load_is_provisioning(user, workspace, tenant, second_tenant):
    _running_load(second_tenant)
    assert _statuses(user, workspace) == ("provisioning", "provisioning")


@pytest.mark.django_db(transaction=True)
def test_a_queued_first_load_is_provisioning(user, workspace, queued_jobs):  # noqa: F811
    materialize_workspace.defer(
        workspace_id=str(workspace.id), user_id="", only_unserved=True, notify_thread=False
    )
    assert _statuses(user, workspace) == ("provisioning", "provisioning")


@pytest.mark.django_db
def test_a_load_retrying_a_failed_view_is_provisioning(user, workspace, tenant, second_tenant):
    WorkspaceViewSchema.objects.create(
        workspace=workspace, schema_name="ws_failed", state=SchemaState.FAILED
    )
    assert _statuses(user, workspace) == ("failed", "failed")
    _running_load(tenant)
    assert _statuses(user, workspace) == ("provisioning", "provisioning")


@pytest.mark.django_db
def test_serving_data_stays_available_during_a_refresh(user, workspace, tenant):
    TenantSchema.objects.create(tenant=tenant, schema_name="serving", state=SchemaState.ACTIVE)
    _running_load(tenant)
    assert _statuses(user, workspace) == ("available", "available")
