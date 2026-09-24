"""Artifact data recovery state, dispatch, and worker integration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth.models import update_last_login
from django.contrib.auth.signals import user_logged_in
from django.test import AsyncClient

from apps.artifacts.models import Artifact, ArtifactType
from apps.chat.models import Thread, ThreadJob
from apps.semantic.models import CubeSchema, SemanticDataset, SemanticField, SemanticModel
from apps.users.models import Tenant, TenantMembership, User
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceDataRecovery,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.query_state import workspace_query_surface
from apps.workspaces.tasks import rebuild_workspace_semantic_model_core, recover_workspace_data
from mcp_server.server import get_schema_status
from tests.tenant_access import agrant_tenant_access, usable_connection


@pytest.fixture
def recovery_setup(db):
    tenant = Tenant.objects.create(
        provider="commcare",
        external_id="recovery-domain",
        canonical_name="Recovery Domain",
    )
    workspace = Workspace.objects.create(name="Recovery Domain")
    WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)
    user = User.objects.create_user(email="recovery@example.com", password="pass")
    TenantMembership.objects.create(
        user=user, tenant=tenant, connection=usable_connection(user, tenant.provider)
    )
    WorkspaceMembership.objects.create(
        workspace=workspace,
        user=user,
        role=WorkspaceRole.MANAGE,
    )
    artifact = Artifact.objects.create(
        workspace=workspace,
        created_by=user,
        title="Visits",
        artifact_type=ArtifactType.STORY,
        code="",
        conversation_id="recovery-thread",
        data={"story_doc": {"schema_version": 1, "blocks": []}},
        semantic_queries=[{"name": "visits", "measures": ["visits.count"]}],
    )
    client = AsyncClient()
    user_logged_in.disconnect(update_last_login)
    try:
        client.force_login(user)
    finally:
        user_logged_in.connect(update_last_login)
    return SimpleNamespace(
        tenant=tenant,
        workspace=workspace,
        user=user,
        artifact=artifact,
        client=client,
        url=f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/recovery/",
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_missing_physical_data_requests_materialization(recovery_setup):
    response = await recovery_setup.client.get(recovery_setup.url)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "needs_materialization"
    assert body["recovery_action"] == "materialization"
    assert body["can_retry"] is True


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_chat_and_artifacts_report_the_same_query_surface(recovery_setup):
    artifact_response = await recovery_setup.client.get(recovery_setup.url)
    chat_response = await get_schema_status(workspace_id=str(recovery_setup.workspace.id))
    surface = chat_response["data"]["query_surface"]
    assert surface["status"] == artifact_response.json()["status"]
    assert surface["recovery_action"] == artifact_response.json()["recovery_action"]
    assert surface["queryable"] is False
    assert surface["in_progress"] is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_ready_physical_data_requests_only_semantic_rebuild(recovery_setup):
    await TenantSchema.objects.acreate(
        tenant=recovery_setup.tenant,
        schema_name="t_recovery_domain",
        state=SchemaState.ACTIVE,
    )

    response = await recovery_setup.client.get(recovery_setup.url)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "needs_semantic_rebuild"
    assert body["recovery_action"] == "semantic_rebuild"
    assert body["physical_status"] == "active"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_active_physical_and_semantic_surfaces_are_ready(recovery_setup):
    await TenantSchema.objects.acreate(
        tenant=recovery_setup.tenant,
        schema_name="t_recovery_domain",
        state=SchemaState.ACTIVE,
    )
    model = await SemanticModel.objects.acreate(
        workspace=recovery_setup.workspace,
        name="Recovery Domain",
        status=SemanticModel.Status.ACTIVE,
        metadata={"last_build": {"ok": True}},
    )
    await CubeSchema.objects.acreate(
        workspace=recovery_setup.workspace,
        semantic_model=model,
        filename="recovery.yaml",
        content="cubes: [{name: visits, measures: [{name: count, type: count}]}]",
        content_hash="ready",
        status=CubeSchema.Status.ACTIVE,
    )
    dataset = await SemanticDataset.objects.acreate(
        workspace=recovery_setup.workspace,
        semantic_model=model,
        name="visits",
        table_name="visits",
        schema_name="t_recovery_domain",
    )
    await SemanticField.objects.acreate(
        dataset=dataset, name="count", field_type="measure", measure_type="count"
    )

    response = await recovery_setup.client.get(recovery_setup.url)

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["recovery_action"] is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_post_dispatches_one_durable_workspace_recovery(recovery_setup):
    defer = AsyncMock(return_value=SimpleNamespace(id=812))
    with patch("apps.artifacts.views.recover_workspace_data.defer_async", new=defer):
        first = await recovery_setup.client.post(recovery_setup.url, data={})
        second = await recovery_setup.client.post(recovery_setup.url, data={})

    assert first.status_code == 202
    assert second.status_code == 200
    assert first.json()["status"] == "recovering"
    assert second.json()["status"] == "recovering"
    assert defer.await_count == 1
    recovery = await WorkspaceDataRecovery.objects.aget(workspace=recovery_setup.workspace)
    assert recovery.source_id == recovery_setup.artifact.id
    assert recovery.procrastinate_job_id == 812
    assert recovery.state == WorkspaceDataRecovery.State.PENDING


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_active_chat_materialization_prevents_duplicate_artifact_recovery(recovery_setup):
    thread = await Thread.objects.acreate(
        workspace=recovery_setup.workspace,
        user=recovery_setup.user,
    )
    await ThreadJob.objects.acreate(
        thread=thread,
        job_type=ThreadJob.JobType.MATERIALIZATION,
        procrastinate_job_id=814,
        tool_call_id="tool-materialize",
    )
    defer = AsyncMock()

    with patch("apps.artifacts.views.recover_workspace_data.defer_async", new=defer):
        response = await recovery_setup.client.post(recovery_setup.url, data={})

    assert response.status_code == 200
    assert response.json()["status"] == "recovering"
    assert response.json()["recovery"]["id"] is None
    defer.assert_not_awaited()
    assert not await WorkspaceDataRecovery.objects.filter(
        workspace=recovery_setup.workspace
    ).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_status_poll_releases_recovery_whose_queue_job_failed(recovery_setup):
    recovery = await WorkspaceDataRecovery.objects.acreate(
        workspace=recovery_setup.workspace,
        requested_by=recovery_setup.user,
        recovery_type=WorkspaceDataRecovery.RecoveryType.MATERIALIZATION,
        source_id=recovery_setup.artifact.id,
        procrastinate_job_id=815,
    )

    with patch(
        "apps.workspaces.tasks._procrastinate_job_status",
        new=AsyncMock(return_value="failed"),
    ):
        response = await recovery_setup.client.get(recovery_setup.url)

    await recovery.arefresh_from_db()
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["can_retry"] is True
    assert "ended (failed)" in response.json()["detail"]
    assert recovery.state == WorkspaceDataRecovery.State.FAILED
    assert "ended (failed)" in recovery.error


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_query_data_returns_recovery_contract_instead_of_generic_query_errors(
    recovery_setup,
):
    query_url = (
        f"/api/workspaces/{recovery_setup.workspace.id}/artifacts/"
        f"{recovery_setup.artifact.id}/query-data/"
    )
    execute = AsyncMock()
    with patch("apps.artifacts.views.run_semantic_query", new=execute):
        response = await recovery_setup.client.get(query_url)

    assert response.status_code == 409
    assert response.json()["data_recovery"]["status"] == "needs_materialization"
    assert response.json()["data_recovery"]["recovery_action"] == "materialization"
    execute.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_deleted_requester_cannot_borrow_other_users_credentials(recovery_setup):
    recovery = await WorkspaceDataRecovery.objects.acreate(
        workspace=recovery_setup.workspace,
        requested_by=recovery_setup.user,
        recovery_type=WorkspaceDataRecovery.RecoveryType.MATERIALIZATION,
    )
    await recovery_setup.user.adelete()
    with patch("apps.workspaces.tasks.materialize_workspace_core", new=AsyncMock()) as materialize:
        result = await recover_workspace_data.func(
            SimpleNamespace(job=SimpleNamespace(id=921)), str(recovery.id)
        )
    await recovery.arefresh_from_db()
    assert result["status"] == "failed"
    assert recovery.state == WorkspaceDataRecovery.State.FAILED
    assert recovery.completed_at is not None
    assert "no longer exists" in recovery.error
    materialize.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_recovery_worker_records_success(recovery_setup):
    recovery = await WorkspaceDataRecovery.objects.acreate(
        workspace=recovery_setup.workspace,
        requested_by=recovery_setup.user,
        recovery_type=WorkspaceDataRecovery.RecoveryType.MATERIALIZATION,
        source_id=recovery_setup.artifact.id,
    )
    context = SimpleNamespace(job=SimpleNamespace(id=913))
    needs_materialization = {
        "status": "needs_materialization",
        "recovery_action": "materialization",
        "message": "Restore data.",
    }
    ready = {
        "status": "ready",
        "recovery_action": None,
        "message": "Ready.",
    }

    with (
        patch(
            "apps.workspaces.tasks._await_in_progress_materializations",
            new=AsyncMock(),
        ),
        patch(
            "apps.workspaces.tasks.recovery_query_surface",
            new=AsyncMock(side_effect=[needs_materialization, ready]),
        ),
        patch(
            "apps.workspaces.tasks.materialize_workspace_core",
            new=AsyncMock(return_value={"cube_schema": {"ok": True}}),
        ) as materialize,
    ):
        result = await recover_workspace_data.func(context, str(recovery.id))

    await recovery.arefresh_from_db()
    assert result["status"] == "completed"
    assert recovery.state == WorkspaceDataRecovery.State.COMPLETED
    assert recovery.procrastinate_job_id == 913
    # A repair reconciles missing sources; it does not force a fresh reload.
    materialize.assert_awaited_once_with(
        str(recovery_setup.workspace.id),
        str(recovery_setup.user.id),
        913,
        intent_kind="reconcile_missing",
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("old_schema_still_serving", [False, True])
async def test_recovery_worker_persists_actionable_failure(
    recovery_setup, old_schema_still_serving
):
    recovery = await WorkspaceDataRecovery.objects.acreate(
        workspace=recovery_setup.workspace,
        requested_by=recovery_setup.user,
        recovery_type=WorkspaceDataRecovery.RecoveryType.SEMANTIC_REBUILD,
        source_id=recovery_setup.artifact.id,
    )
    context = SimpleNamespace(job=SimpleNamespace(id=914))
    needs_semantic = {
        "status": "needs_semantic_rebuild",
        "recovery_action": "semantic_rebuild",
        "message": "Rebuild semantics.",
    }

    with (
        patch(
            "apps.workspaces.tasks.recovery_query_surface",
            new=AsyncMock(
                side_effect=[
                    needs_semantic,
                    {"status": "ready", "recovery_action": None, "message": "Ready."}
                    if old_schema_still_serving
                    else needs_semantic,
                ]
            ),
        ),
        patch(
            "apps.workspaces.tasks.rebuild_workspace_semantic_model_core",
            new=AsyncMock(
                return_value={"cube_schema": {"ok": False, "error": "Cube rejected schema"}}
            ),
        ),
    ):
        result = await recover_workspace_data.func(context, str(recovery.id))

    await recovery.arefresh_from_db()
    assert result["status"] == "failed"
    assert recovery.state == WorkspaceDataRecovery.State.FAILED
    assert recovery.error == "Cube rejected schema"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_missing_view_keeps_existing_source_data(recovery_setup):
    other = await Tenant.objects.acreate(provider="commcare", external_id="other-recovery")
    await WorkspaceTenant.objects.acreate(workspace=recovery_setup.workspace, tenant=other)
    await agrant_tenant_access(recovery_setup.user, other)
    await TenantSchema.objects.acreate(
        tenant=recovery_setup.tenant, schema_name="recovery_present", state=SchemaState.ACTIVE
    )
    await WorkspaceViewSchema.objects.acreate(
        workspace=recovery_setup.workspace, schema_name="ws_recovery", state=SchemaState.EXPIRED
    )
    response = await recovery_setup.client.get(recovery_setup.url)
    assert response.json()["recovery_action"] == "view_rebuild"
    recovery = await WorkspaceDataRecovery.objects.acreate(
        workspace=recovery_setup.workspace,
        requested_by=recovery_setup.user,
        recovery_type=WorkspaceDataRecovery.RecoveryType.VIEW_REBUILD,
    )
    with (
        patch(
            "apps.workspaces.tasks.recovery_query_surface",
            new=AsyncMock(side_effect=[response.json(), {"status": "ready"}]),
        ),
        patch(
            "apps.workspaces.tasks.rebuild_workspace_view_schema.func",
            new=AsyncMock(return_value={"cube_schema": {"ok": True}}),
        ) as rebuild,
        patch("apps.workspaces.tasks.materialize_workspace_core", new=AsyncMock()) as materialize,
    ):
        result = await recover_workspace_data.func(
            SimpleNamespace(job=SimpleNamespace(id=920)), str(recovery.id)
        )
    assert result["status"] == "completed"
    rebuild.assert_awaited_once_with(str(recovery_setup.workspace.id))
    materialize.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["failed", "partial", "cancelled"])
async def test_semantic_rebuild_rejects_unsafe_snapshot(recovery_setup, state):
    schema = await TenantSchema.objects.acreate(
        tenant=recovery_setup.tenant, schema_name="recovery_unsafe", state=SchemaState.ACTIVE
    )
    await MaterializationRun.objects.acreate(tenant_schema=schema, pipeline="test", state=state)
    surface = await workspace_query_surface(recovery_setup.workspace)
    assert surface["recovery_action"] == "materialization"
    with patch("apps.workspaces.tasks.build_and_promote_cube_schema") as promote:
        result = await rebuild_workspace_semantic_model_core(str(recovery_setup.workspace.id))
    assert result["cube_schema"]["ok"] is False
    assert "unsafe" in result["cube_schema"]["error"]
    promote.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_semantic_rebuild_defers_for_active_snapshot(recovery_setup):
    schema = await TenantSchema.objects.acreate(
        tenant=recovery_setup.tenant, schema_name="recovery_loading", state=SchemaState.ACTIVE
    )
    await MaterializationRun.objects.acreate(tenant_schema=schema, pipeline="test", state="loading")
    surface = await workspace_query_surface(recovery_setup.workspace)
    assert surface["status"] == "recovering"
    assert surface["in_progress"] is True
    assert surface["queryable"] is False
    with patch("apps.workspaces.tasks.build_and_promote_cube_schema") as promote:
        result = await rebuild_workspace_semantic_model_core(str(recovery_setup.workspace.id))
    assert result["cube_schema"]["status"] == "deferred"
    promote.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_shared_query_state_keeps_stale_build_diagnostics(recovery_setup):
    await TenantSchema.objects.acreate(
        tenant=recovery_setup.tenant, schema_name="recovery_stale", state=SchemaState.ACTIVE
    )
    model = await SemanticModel.objects.acreate(
        workspace=recovery_setup.workspace,
        name="Stale",
        status=SemanticModel.Status.ACTIVE,
        metadata={
            "last_build": {"ok": False, "status": "deferred", "error": "Previous build failed"}
        },
    )
    await CubeSchema.objects.acreate(
        workspace=recovery_setup.workspace,
        semantic_model=model,
        status=CubeSchema.Status.ACTIVE,
        filename="stale.yaml",
        content="cubes: []",
        content_hash="stale",
    )
    surface = await workspace_query_surface(recovery_setup.workspace)
    assert surface["status"] == "ready"
    assert surface["semantic_status"] == "stale"
    assert surface["detail"] == "Previous build failed"
