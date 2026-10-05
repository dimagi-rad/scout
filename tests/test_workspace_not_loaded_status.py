"""A workspace with nothing serving reports ``not_loaded`` unless a load is running (#249 05#4).

``provisioning`` used to stand for "not serving yet", so a multi-source workspace
that was never loaded, or a source whose load died mid-way, showed a permanent
"Loading data..." spinner. Every surface that reports the status is pinned here.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.graph.prompt_context import _fetch_semantic_model_context
from apps.semantic.services.catalog import SemanticCatalogUnavailable, load_physical_tables
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.load_activity import (
    _STATUS_TASK_NAMES,
    MATERIALIZE_TASK_NAME,
    REBUILD_VIEW_TASK_NAME,
    workspace_schema_statuses,
)
from apps.workspaces.tasks import (
    materialize_workspace,
    rebuild_workspace_view_schema,
)
from mcp_server.server import get_schema_status
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


def _running_load(tenant):
    schema = TenantSchema.objects.create(
        tenant=tenant,
        schema_name=f"s_{tenant.external_id}".replace("-", "_"),
        state=SchemaState.PROVISIONING,
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
def test_a_running_first_load_is_provisioning(user, workspace, second_tenant):
    _running_load(second_tenant)
    assert _statuses(user, workspace) == ("provisioning", "provisioning")


@pytest.mark.django_db(transaction=True)
def test_a_queued_first_load_is_provisioning(user, workspace, queued_jobs):  # noqa: F811
    materialize_workspace.defer(
        workspace_id=str(workspace.id), user_id="", only_unserved=True, notify_thread=False
    )
    assert _statuses(user, workspace) == ("provisioning", "provisioning")


@pytest.mark.django_db(transaction=True)
def test_a_queued_view_rebuild_is_provisioning(
    user,
    workspace,
    tenant,
    second_tenant,
    queued_jobs,  # noqa: F811
):
    """Adding a source to a serving workspace queues only a view rebuild, no load."""
    TenantSchema.objects.create(tenant=tenant, schema_name="serving", state=SchemaState.ACTIVE)
    WorkspaceViewSchema.objects.create(
        workspace=workspace, schema_name="ws_rebuilding", state=SchemaState.PROVISIONING
    )
    rebuild_workspace_view_schema.defer(workspace_id=str(workspace.id))
    assert _statuses(user, workspace) == ("provisioning", "provisioning")


def test_status_build_tasks_match_the_view_build_tasks():
    """load_activity cannot import tasks, so its copy of the names must be pinned."""
    assert rebuild_workspace_view_schema.name == REBUILD_VIEW_TASK_NAME
    assert materialize_workspace.name == MATERIALIZE_TASK_NAME
    assert set(_STATUS_TASK_NAMES) == {MATERIALIZE_TASK_NAME, REBUILD_VIEW_TASK_NAME}


@pytest.mark.django_db
def test_bulk_status_keeps_each_workspace_s_load_to_itself(user, workspace, tenant):
    other = Workspace.objects.create(name="Other", created_by=user)
    other_tenant = Tenant.objects.create(provider="commcare", external_id="other-domain")
    WorkspaceTenant.objects.create(workspace=other, tenant=other_tenant)
    _running_load(other_tenant)

    assert workspace_schema_statuses([workspace.id, other.id]) == {
        workspace.id: "not_loaded",
        other.id: "provisioning",
    }


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


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_reports_not_loaded_for_a_never_loaded_multi_source_workspace():
    user = await get_user_model().objects.acreate_user(email="mcp-nl@b.c", password="x")
    workspace = await Workspace.objects.acreate(name="Never loaded", created_by=user)
    for external_id in ("alpha", "bravo"):
        tenant = await Tenant.objects.acreate(provider="commcare", external_id=external_id)
        await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=tenant)

    result = await get_schema_status(workspace_id=str(workspace.id))

    assert result["success"] is True
    assert result["data"]["exists"] is False
    assert result["data"]["state"] == "not_loaded"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_reports_provisioning_while_a_first_load_runs():
    user = await get_user_model().objects.acreate_user(email="mcp-prov@b.c", password="x")
    workspace = await Workspace.objects.acreate(name="Loading", created_by=user)
    tenant = await Tenant.objects.acreate(provider="commcare", external_id="loading")
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=tenant)
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="loading", state=SchemaState.PROVISIONING
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema, pipeline="commcare_sync", state=LOADING
    )

    result = await get_schema_status(workspace_id=str(workspace.id))

    assert result["success"] is True
    assert result["data"]["state"] == "provisioning"


def _catalog_status(workspace):
    with pytest.raises(SemanticCatalogUnavailable) as caught:
        load_physical_tables(workspace)
    return caught.value.schema_status


@pytest.mark.django_db
def test_catalog_judges_a_multi_source_workspace_by_all_of_its_sources(workspace, second_tenant):
    """The first source alone used to decide; a load of any other source went unseen."""
    assert _catalog_status(workspace) == "not_loaded"
    _running_load(second_tenant)
    assert _catalog_status(workspace) == "provisioning"


@pytest.mark.django_db
def test_catalog_ignores_a_stranded_provisioning_first_source(workspace, tenant, second_tenant):
    TenantSchema.objects.create(
        tenant=tenant, schema_name="stranded", state=SchemaState.PROVISIONING
    )
    assert _catalog_status(workspace) == "not_loaded"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_read_only_prompt_says_a_writer_s_first_chat_starts_the_load(workspace):
    prompt = await _fetch_semantic_model_context(workspace, write_capable=False)
    assert "starts the load automatically" in prompt


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_never_loaded_multi_source_prompt_asks_for_a_load(workspace):
    other = await Tenant.objects.acreate(provider="commcare", external_id="prompt-second")
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=other)

    prompt = await _fetch_semantic_model_context(workspace, write_capable=True)

    assert "No data has been loaded yet" in prompt
    assert "run_materialization" in prompt
