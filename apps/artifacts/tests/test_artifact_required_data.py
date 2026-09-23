"""Required-source recovery with real catalog, endpoint, queue-state and lock rows."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from django.contrib.auth.models import update_last_login
from django.contrib.auth.signals import user_logged_in
from django.test import AsyncClient
from django.utils import timezone

from apps.artifacts.models import Artifact, ArtifactType
from apps.common.error_codes import ErrorCode
from apps.semantic.models import (
    CubeSchema,
    CustomDataset,
    SemanticDataset,
    SemanticField,
    SemanticModel,
)
from apps.semantic.services.catalog import PhysicalTable, ensure_semantic_model
from apps.semantic.services.cube import generate_cube_schema
from apps.semantic.services.query import SemanticQueryError, _compile_semantic_query
from apps.users.models import Tenant, TenantMembership, User
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceDataRecovery,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.data_operation import workspace_data_lock
from apps.workspaces.services.data_recovery import artifact_data_state
from apps.workspaces.services.schema_manager import SchemaManager
from apps.workspaces.tasks import reconcile_workspace_data_recovery, recover_workspace_data

pytestmark = [pytest.mark.django_db(transaction=True)]


@pytest.fixture
def required_setup():
    workspace = Workspace.objects.create(name="Required-source test")
    user = User.objects.create_user(email="required-sources@example.com", password="test-only")
    WorkspaceMembership.objects.create(workspace=workspace, user=user, role=WorkspaceRole.MANAGE)
    tenants = [
        Tenant.objects.create(
            provider="commcare", external_id=f"required-{key}", canonical_name=f"Source-{key}"
        )
        for key in ("A", "B")
    ]
    for tenant in tenants:
        WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)
        TenantMembership.objects.create(user=user, tenant=tenant)
    schema_a = TenantSchema.objects.create(
        tenant=tenants[0], schema_name="required_source_a", state=SchemaState.ACTIVE
    )
    schema_b = TenantSchema.objects.create(
        tenant=tenants[1], schema_name="required_source_b", state=SchemaState.EXPIRED
    )
    coverage = {
        "included_tenants": [SchemaManager._tenant_coverage_entry(tenants[0])],
        "excluded_tenants": [SchemaManager._tenant_coverage_entry(tenants[1])],
    }
    view = WorkspaceViewSchema.objects.create(
        workspace=workspace,
        schema_name="ws_required_sources",
        state=SchemaState.ACTIVE,
        tenant_coverage=coverage,
    )
    tables = [
        PhysicalTable(
            name=f"source_{key}__visits",
            type="view",
            description="Synthetic visits",
            columns=[{"name": "category", "type": "text"}],
            source_tenant_ids=(str(tenant.id),),
        )
        for key, tenant in zip(("a", "b"), tenants, strict=True)
    ]
    with patch(
        "apps.semantic.services.catalog.load_physical_tables",
        return_value=(view.schema_name, tables),
    ):
        model = ensure_semantic_model(workspace)
    # This is the real degraded-refresh catalog path: A remains, B disappears.
    with patch(
        "apps.semantic.services.catalog.load_physical_tables",
        return_value=(view.schema_name, tables[:1]),
    ):
        ensure_semantic_model(workspace)
    model.metadata = {"last_build": {"ok": True, "at": timezone.now().isoformat()}}
    model.save(update_fields=["metadata"])
    cube = CubeSchema.objects.create(
        workspace=workspace,
        semantic_model=model,
        filename="required.yaml",
        content=yaml.safe_dump(generate_cube_schema(model)),
        content_hash="required",
        status=CubeSchema.Status.ACTIVE,
    )
    artifacts = [
        Artifact.objects.create(
            workspace=workspace,
            created_by=user,
            title=f"Source {key.upper()} visits",
            artifact_type=ArtifactType.STORY,
            code="",
            semantic_queries=[{"name": "visits", "measures": [f"source_{key}__visits.count"]}],
        )
        for key in ("a", "b")
    ]
    client = AsyncClient()
    user_logged_in.disconnect(update_last_login)
    try:
        client.force_login(user)
    finally:
        user_logged_in.connect(update_last_login)
    return SimpleNamespace(
        workspace=workspace,
        user=user,
        tenants=tenants,
        schema_a=schema_a,
        schema_b=schema_b,
        view=view,
        model=model,
        cube=cube,
        tables=tables,
        a=artifacts[0],
        b=artifacts[1],
        client=client,
    )


def recovery_url(artifact):
    return f"/api/workspaces/{artifact.workspace_id}/artifacts/{artifact.id}/recovery/"


def query_url(artifact):
    return f"/api/workspaces/{artifact.workspace_id}/artifacts/{artifact.id}/query-data/"


async def make_recovery(setup, *, artifact=None, recovery_type="materialization"):
    return await WorkspaceDataRecovery.objects.acreate(
        workspace=setup.workspace,
        requested_by=setup.user,
        source_type="artifact",
        source_id=(artifact or setup.b).id,
        recovery_type=recovery_type,
    )


def task_context(job_id=901):
    return SimpleNamespace(job=SimpleNamespace(id=job_id))


async def restore_b(setup):
    await TenantSchema.objects.filter(id=setup.schema_b.id).aupdate(state=SchemaState.ACTIVE)
    await WorkspaceViewSchema.objects.filter(id=setup.view.id).aupdate(
        tenant_coverage={
            "included_tenants": [
                SchemaManager._tenant_coverage_entry(tenant) for tenant in setup.tenants
            ],
            "excluded_tenants": [],
        }
    )
    await SemanticDataset.objects.filter(
        workspace=setup.workspace, name="source_b__visits"
    ).aupdate(is_visible=True)
    await CubeSchema.objects.filter(id=setup.cube.id).aupdate(
        content="cubes: [{name: source_a__visits, measures: [{name: count, type: count}], dimensions: [{name: category, type: string}]}, {name: source_b__visits, measures: [{name: count, type: count}], dimensions: [{name: category, type: string}]}]"
    )
    await SemanticModel.objects.filter(id=setup.model.id).aupdate(
        metadata={"last_build": {"ok": True, "at": timezone.now().isoformat()}}
    )


def test_degraded_catalog_reproduces_actual_query_dependency_failure(required_setup):
    setup = required_setup
    assert _compile_semantic_query(setup.workspace, setup.a.semantic_queries[0])["members"] == [
        "source_a__visits.count"
    ]
    with pytest.raises(SemanticQueryError, match="Unknown dataset 'source_b__visits'"):
        _compile_semantic_query(setup.workspace, setup.b.semantic_queries[0])
    assert SemanticDataset.objects.get(workspace=setup.workspace, name="source_b__visits").metadata[
        "source_tenant_ids"
    ] == [str(setup.tenants[1].id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("old_catalog_still_visible", [False, True])
async def test_required_excluded_source_repairs_b_but_not_healthy_a(
    required_setup, legacy, old_catalog_still_visible
):
    setup = required_setup
    if legacy:
        await SemanticDataset.objects.filter(workspace=setup.workspace).aupdate(metadata={})
    if old_catalog_still_visible:
        await SemanticDataset.objects.filter(
            workspace=setup.workspace, name="source_b__visits"
        ).aupdate(is_visible=True)
    defer = AsyncMock(return_value=SimpleNamespace(id=902))
    with patch("apps.artifacts.views.recover_workspace_data.defer_async", new=defer):
        healthy = await setup.client.get(recovery_url(setup.a))
        healthy_post = await setup.client.post(recovery_url(setup.a), data={})
        broken = await setup.client.get(recovery_url(setup.b))
        assert healthy.json()["status"] == healthy_post.json()["status"] == "ready"
        assert healthy.json()["queryable"] is True
        assert healthy.json()["recovery_action"] is None
        assert broken.json()["status"] == "needs_materialization"
        assert broken.json()["queryable"] is False
        defer.assert_not_awaited()
        queued = await setup.client.post(recovery_url(setup.b), data={})
    assert queued.status_code == 202
    defer.assert_awaited_once()
    recovery = await WorkspaceDataRecovery.objects.aget(workspace=setup.workspace)
    assert recovery.source_id == setup.b.id
    assert recovery.recovery_type == "materialization"


@pytest.mark.asyncio
async def test_get_is_read_only_for_catalog_and_required_query_is_gated(required_setup):
    setup = required_setup
    before = [
        row async for row in SemanticDataset.objects.filter(workspace=setup.workspace).values()
    ]
    with patch("apps.artifacts.views.run_semantic_query", new=AsyncMock()) as query:
        response = await setup.client.get(query_url(setup.b))
    assert response.status_code == 409
    assert response.json()["data_recovery"]["recovery_action"] == "materialization"
    query.assert_not_awaited()
    assert before == [
        row async for row in SemanticDataset.objects.filter(workspace=setup.workspace).values()
    ]
    assert not await WorkspaceDataRecovery.objects.filter(workspace=setup.workspace).aexists()


@pytest.mark.asyncio
async def test_required_source_already_loaded_only_rebuilds_view(required_setup):
    setup = required_setup
    await TenantSchema.objects.filter(id=setup.schema_b.id).aupdate(state=SchemaState.ACTIVE)
    response = await setup.client.get(recovery_url(setup.b))
    assert response.json()["recovery_action"] == "view_rebuild"
    recovery = await make_recovery(setup)

    async def rebuild(_workspace_id):
        await restore_b(setup)
        return {"cube_schema": {"ok": True}}

    with (
        patch(
            "apps.workspaces.tasks.rebuild_workspace_view_schema.func",
            new=AsyncMock(side_effect=rebuild),
        ) as view,
        patch("apps.workspaces.tasks.materialize_workspace_core", new=AsyncMock()) as load,
    ):
        result = await recover_workspace_data.func(task_context(), str(recovery.id))
    assert result["status"] == "completed"
    view.assert_awaited_once_with(str(setup.workspace.id))
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_view_does_not_mask_required_missing_source(required_setup):
    setup = required_setup
    await WorkspaceViewSchema.objects.filter(id=setup.view.id).aupdate(state=SchemaState.EXPIRED)
    assert (await artifact_data_state(setup.a))["recovery_action"] == "view_rebuild"
    assert (await artifact_data_state(setup.b))["recovery_action"] == "materialization"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["field_deleted", "field_hidden", "field_type", "dataset_deleted", "dataset_curated_hidden"],
)
async def test_model_drift_does_not_authorize_provider_reload(required_setup, change):
    setup = required_setup
    dataset = await SemanticDataset.objects.aget(workspace=setup.workspace, name="source_b__visits")
    if change == "field_deleted":
        await SemanticField.objects.filter(dataset=dataset, name="count").adelete()
    elif change == "field_hidden":
        await SemanticField.objects.filter(dataset=dataset, name="count").aupdate(is_visible=False)
    elif change == "field_type":
        await SemanticField.objects.filter(dataset=dataset, name="count").aupdate(
            field_type="dimension"
        )
    elif change == "dataset_deleted":
        await dataset.adelete()
    else:
        await SemanticDataset.objects.filter(id=dataset.id).aupdate(
            metadata={**dataset.metadata, "curated_fields": ["is_visible"]}
        )
    with patch("apps.artifacts.views.recover_workspace_data.defer_async", new=AsyncMock()) as defer:
        response = await setup.client.post(recovery_url(setup.b), data={})
    assert response.status_code == 409
    assert response.json()["data_recovery"]["status"] == "model_drift"
    assert response.json()["data_recovery"]["recovery_action"] is None
    assert "chat" in response.json()["data_recovery"]["detail"]
    defer.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_ambiguous_source_is_not_guessed(required_setup):
    setup = required_setup
    await SemanticDataset.objects.filter(
        workspace=setup.workspace, name="source_b__visits"
    ).aupdate(metadata={})
    await Tenant.objects.filter(id=setup.tenants[0].id).aupdate(canonical_name="Source-B")
    state = await artifact_data_state(setup.b)
    assert state["status"] == "needs_view_rebuild"
    assert "identified safely" in state["message"]
    assert state["recovery_action"] == "view_rebuild"
    assert state["queryable"] is False


@pytest.mark.asyncio
async def test_unmapped_historical_dataset_requires_verified_catalog(required_setup):
    setup = required_setup
    await SemanticDataset.objects.filter(
        workspace=setup.workspace, name="source_a__visits"
    ).aupdate(metadata={}, schema_name="historical_schema", table_name="legacy_visits")
    state = await artifact_data_state(setup.a)
    assert state["status"] == "needs_semantic_rebuild"
    assert state["queryable"] is False
    assert state["recovery_action"] == "semantic_rebuild"


@pytest.mark.asyncio
async def test_legacy_single_tenant_schema_owner_is_authoritative(required_setup):
    setup = required_setup
    await WorkspaceTenant.objects.filter(
        workspace=setup.workspace, tenant=setup.tenants[0]
    ).adelete()
    await SemanticDataset.objects.filter(
        workspace=setup.workspace, name="source_b__visits"
    ).aupdate(metadata={}, schema_name=setup.schema_b.schema_name)
    assert (await artifact_data_state(setup.b))["recovery_action"] == "materialization"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "definition",
    [
        "SELECT category FROM source_b__visits",
        "WITH visits AS (SELECT category FROM source_b__visits) SELECT * FROM visits",
        "SELECT a.category FROM source_a__visits a JOIN source_b__visits b ON a.category = b.category",
    ],
)
async def test_custom_dataset_follows_actual_physical_sql_dependencies(required_setup, definition):
    setup = required_setup
    custom = await CustomDataset.objects.acreate(
        workspace=setup.workspace,
        name="combined_visits",
        definition_sql=definition,
        status=CustomDataset.Status.ERROR,
    )
    dataset = await SemanticDataset.objects.acreate(
        workspace=setup.workspace,
        semantic_model=setup.model,
        source_kind="custom",
        custom_dataset=custom,
        name=custom.name,
        table_name=custom.name,
        is_visible=False,
    )
    await SemanticField.objects.acreate(
        dataset=dataset, name="count", field_type="measure", measure_type="count"
    )
    await Artifact.objects.filter(id=setup.b.id).aupdate(
        semantic_queries=[{"measures": ["combined_visits.count"]}]
    )
    await setup.b.arefresh_from_db(fields=["semantic_queries"])
    state = await artifact_data_state(setup.b)
    assert state["recovery_action"] == "materialization"
    assert state["queryable"] is False
    await CustomDataset.objects.filter(id=custom.id).aupdate(status=CustomDataset.Status.DRAFT)
    assert (await artifact_data_state(setup.b))["status"] == "model_drift"


def test_catalog_revalidates_failed_custom_dependency_after_source_returns(required_setup):
    setup = required_setup
    custom = CustomDataset.objects.create(
        workspace=setup.workspace,
        name="custom_visits",
        definition_sql="SELECT category FROM source_b__visits",
    )
    with (
        patch(
            "apps.semantic.services.catalog.load_physical_tables",
            return_value=(setup.view.schema_name, setup.tables[:1]),
        ),
        patch(
            "apps.semantic.services.catalog.infer_custom_dataset_columns",
            return_value=[{"name": "category", "type": "text"}],
        ),
    ):
        ensure_semantic_model(setup.workspace)
    custom.refresh_from_db()
    assert custom.status == CustomDataset.Status.ERROR
    with (
        patch(
            "apps.semantic.services.catalog.load_physical_tables",
            return_value=(setup.view.schema_name, setup.tables),
        ),
        patch(
            "apps.semantic.services.catalog.infer_custom_dataset_columns",
            return_value=[{"name": "category", "type": "text"}],
        ),
    ):
        ensure_semantic_model(setup.workspace)
    custom.refresh_from_db()
    assert custom.status == CustomDataset.Status.ACTIVE
    assert custom.diagnostics == []
    assert SemanticDataset.objects.get(custom_dataset=custom).is_visible


@pytest.mark.asyncio
@pytest.mark.parametrize("required_source_repaired", [False, True])
async def test_partial_worker_result_is_success_only_when_requested_artifact_repaired(
    required_setup, required_source_repaired
):
    setup = required_setup
    recovery = await make_recovery(setup)

    async def materialize(*_args):
        if required_source_repaired:
            await restore_b(setup)
        # A partial result alone is not failure: a different/unreferenced load
        # may have failed while all dependencies of this artifact were repaired.
        return {
            "all_succeeded": False,
            "tenants": [{"ok": False, "error": "Synthetic provider denied access"}],
            "cube_schema": {"ok": True},
        }

    with patch(
        "apps.workspaces.tasks.materialize_workspace_core", new=AsyncMock(side_effect=materialize)
    ) as load:
        result = await recover_workspace_data.func(task_context(), str(recovery.id))
        await recover_workspace_data.func(task_context(), str(recovery.id))
    await recovery.arefresh_from_db()
    expected = "completed" if required_source_repaired else "failed"
    assert result["status"] == recovery.state == expected
    assert recovery.completed_at is not None
    load.assert_awaited_once()
    if not required_source_repaired:
        assert "denied access" in recovery.error
        assert (await artifact_data_state(setup.b))["can_retry"] is True
        healthy = await artifact_data_state(setup.a)
        assert healthy["status"] == "ready"
        assert healthy["recovery_action"] is None


@pytest.mark.asyncio
async def test_worker_reassesses_exact_artifact_after_waiting_for_workspace_lock(required_setup):
    setup = required_setup
    recovery = await make_recovery(setup)
    attempted = asyncio.Event()

    @asynccontextmanager
    async def observed_lock(workspace_id):
        attempted.set()
        async with workspace_data_lock(workspace_id):
            yield

    with (
        patch("apps.workspaces.tasks.materialize_workspace_core", new=AsyncMock()) as load,
        patch("apps.workspaces.tasks.workspace_data_lock", new=observed_lock),
    ):
        async with workspace_data_lock(setup.workspace.id):
            worker = asyncio.create_task(
                recover_workspace_data.func(task_context(), str(recovery.id))
            )
            await asyncio.wait_for(attempted.wait(), timeout=2)
            assert await WorkspaceDataRecovery.objects.filter(
                id=recovery.id, state="running"
            ).aexists()
            assert not worker.done()
            await restore_b(setup)
        result = await asyncio.wait_for(worker, timeout=2)
    assert result["status"] == "completed"
    assert result["result"]["status"] == "already_recovered"
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_recovery_posts_keep_one_durable_request(required_setup):
    setup = required_setup
    with patch(
        "apps.artifacts.views.recover_workspace_data.defer_async",
        new=AsyncMock(return_value=SimpleNamespace(id=909)),
    ) as defer:
        responses = await asyncio.gather(
            *(setup.client.post(recovery_url(setup.b), data={}) for _ in range(2))
        )
    assert all(response.status_code in {200, 202} for response in responses)
    assert await WorkspaceDataRecovery.objects.filter(workspace=setup.workspace).acount() == 1
    defer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["workspace_membership", "tenant_access", "artifact_deleted", "artifact_moved"]
)
async def test_queued_repair_rechecks_authority_and_source_scope(required_setup, change):
    setup = required_setup
    recovery = await make_recovery(setup)
    if change == "workspace_membership":
        await WorkspaceMembership.objects.filter(
            workspace=setup.workspace, user=setup.user
        ).adelete()
    elif change == "tenant_access":
        await TenantMembership.objects.filter(user=setup.user).adelete()
    elif change == "artifact_deleted":
        await Artifact.objects.filter(id=setup.b.id).adelete()
    else:
        other = await Workspace.objects.acreate(name="Other synthetic workspace")
        await Artifact.objects.filter(id=setup.b.id).aupdate(workspace=other)
    with patch("apps.workspaces.tasks.materialize_workspace_core", new=AsyncMock()) as load:
        result = await recover_workspace_data.func(task_context(), str(recovery.id))
    await recovery.arefresh_from_db()
    assert result["status"] == recovery.state == "failed"
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_endpoint_does_not_disclose_inaccessible_artifact(required_setup):
    setup = required_setup
    await WorkspaceMembership.objects.filter(workspace=setup.workspace, user=setup.user).adelete()
    assert (await setup.client.get(recovery_url(setup.b))).status_code == 403
    assert (await setup.client.post(recovery_url(setup.b), data={})).status_code == 403
    assert not await WorkspaceDataRecovery.objects.filter(workspace=setup.workspace).aexists()


@pytest.mark.asyncio
async def test_successful_queue_status_cannot_certify_missing_requested_source(required_setup):
    setup = required_setup
    recovery = await make_recovery(setup)
    recovery.procrastinate_job_id = 903
    await recovery.asave(update_fields=["procrastinate_job_id"])
    with patch(
        "apps.workspaces.tasks._procrastinate_job_status", new=AsyncMock(return_value="succeeded")
    ):
        assert await reconcile_workspace_data_recovery(recovery) == "failed"
    await recovery.arefresh_from_db()
    assert recovery.state == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("build_failure_recorded", [False, True])
async def test_readable_failure_is_disclosed_until_later_verified_repair(
    required_setup, build_failure_recorded
):
    setup = required_setup
    if build_failure_recorded:
        await SemanticModel.objects.filter(id=setup.model.id).aupdate(
            metadata={
                "last_build": {
                    "ok": False,
                    "at": timezone.now().isoformat(),
                    "error": "Synthetic Cube rejection",
                }
            }
        )
    recovery = await make_recovery(setup, artifact=setup.a, recovery_type="semantic_rebuild")
    await WorkspaceDataRecovery.objects.filter(id=recovery.id).aupdate(
        state="failed", completed_at=timezone.now(), error="Synthetic repair failed"
    )
    failed = await setup.client.get(recovery_url(setup.a))
    assert failed.json()["status"] == "failed"
    assert failed.json()["queryable"] is True
    assert failed.json()["recovery_action"] == "semantic_rebuild"
    with patch(
        "apps.artifacts.views.run_semantic_query",
        new=AsyncMock(return_value={"columns": ["count"], "rows": [[4]], "row_count": 1}),
    ) as query:
        assert (await setup.client.get(query_url(setup.a))).status_code == 200
        query.assert_awaited_once()
    with patch(
        "apps.artifacts.views.recover_workspace_data.defer_async",
        new=AsyncMock(return_value=SimpleNamespace(id=904)),
    ):
        queued = await setup.client.post(recovery_url(setup.a), data={})
    assert queued.status_code == 202
    assert queued.json()["queryable"] is True

    async def rebuild(*_args):
        await SemanticModel.objects.filter(id=setup.model.id).aupdate(
            metadata={"last_build": {"ok": True, "at": timezone.now().isoformat()}}
        )
        return {"cube_schema": {"ok": True}}

    current = await WorkspaceDataRecovery.objects.aget(workspace=setup.workspace, state="pending")
    with patch(
        "apps.workspaces.tasks.rebuild_workspace_semantic_model_core",
        new=AsyncMock(side_effect=rebuild),
    ) as build:
        result = await recover_workspace_data.func(task_context(904), str(current.id))
    build.assert_awaited_once()
    assert result["status"] == "completed"
    repaired = await setup.client.get(recovery_url(setup.a))
    assert repaired.json()["status"] == "ready"
    assert repaired.json()["recovery_action"] is None
    assert repaired.json()["recovery"] is None
    assert "detail" not in repaired.json()


@pytest.mark.asyncio
async def test_catalog_member_not_promoted_is_blocking_but_old_member_remains_readable(
    required_setup,
):
    setup = required_setup
    dataset = await SemanticDataset.objects.aget(workspace=setup.workspace, name="source_a__visits")
    await SemanticField.objects.acreate(
        dataset=dataset, name="new_category", field_type="dimension", expression="new_category"
    )
    await SemanticModel.objects.filter(id=setup.model.id).aupdate(
        metadata={"last_build": {"ok": False, "error": "Synthetic promotion failed"}}
    )
    await Artifact.objects.filter(id=setup.b.id).aupdate(
        semantic_queries=[
            {
                "measures": ["source_a__visits.count"],
                "dimensions": ["source_a__visits.new_category"],
            }
        ]
    )
    old = await setup.client.get(recovery_url(setup.a))
    new = await setup.client.get(recovery_url(setup.b))
    assert old.json()["queryable"] is True
    assert old.json()["recovery_action"] == "semantic_rebuild"
    assert new.json()["queryable"] is False
    assert new.json()["recovery_action"] == "semantic_rebuild"
    assert "serving data model" in new.json()["message"]
    with patch("apps.artifacts.views.run_semantic_query", new=AsyncMock()) as query:
        assert (await setup.client.get(query_url(setup.b))).status_code == 409
    query.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_between_request_creation_and_failure_does_not_clear_warning(required_setup):
    setup = required_setup
    recovery = await make_recovery(setup, artifact=setup.a, recovery_type="semantic_rebuild")
    between = timezone.now()
    await SemanticModel.objects.filter(id=setup.model.id).aupdate(
        metadata={"last_build": {"ok": True, "at": between.isoformat()}}
    )
    finished = between + timedelta(seconds=1)
    await WorkspaceDataRecovery.objects.filter(id=recovery.id).aupdate(
        state="failed", completed_at=finished, error="Later synthetic failure"
    )
    warning = await artifact_data_state(setup.a)
    assert warning["queryable"] is True
    assert warning["status"] == "failed"
    assert warning["recovery_action"] == "semantic_rebuild"
    await SemanticModel.objects.filter(id=setup.model.id).aupdate(
        metadata={"last_build": {"ok": True, "at": (finished + timedelta(seconds=1)).isoformat()}}
    )
    cleared = await artifact_data_state(setup.a)
    assert cleared["status"] == "ready"
    assert cleared["recovery_action"] is None


@pytest.mark.asyncio
async def test_new_publication_invalidates_cached_iframe_query_rows(required_setup):
    setup = required_setup
    with patch(
        "apps.artifacts.views.run_semantic_query",
        new=AsyncMock(return_value={"columns": ["count"], "rows": [[4]], "row_count": 1}),
    ) as query:
        first = await setup.client.get(query_url(setup.a))
        cached = await setup.client.get(query_url(setup.a))
        query.assert_awaited_once()
        assert first.json()["queries"] == cached.json()["queries"]
        before = (await artifact_data_state(setup.a))["data_revision"]
        await CubeSchema.objects.filter(id=setup.cube.id).aupdate(updated_at=timezone.now())
        after = (await artifact_data_state(setup.a))["data_revision"]
        assert before != after
        query.return_value = {"columns": ["count"], "rows": [[9]], "row_count": 1}
        refreshed = await setup.client.get(query_url(setup.a))
        assert query.await_count == 2
        assert refreshed.json()["queries"][0]["rows"] == [[9]]


@pytest.mark.asyncio
async def test_restored_source_with_rolled_back_catalog_retries_only_semantic_promotion(
    required_setup,
):
    setup = required_setup
    await TenantSchema.objects.filter(id=setup.schema_b.id).aupdate(state=SchemaState.ACTIVE)
    await WorkspaceViewSchema.objects.filter(id=setup.view.id).aupdate(
        tenant_coverage={
            "included_tenants": [
                SchemaManager._tenant_coverage_entry(tenant) for tenant in setup.tenants
            ],
            "excluded_tenants": [],
        }
    )
    await SemanticModel.objects.filter(id=setup.model.id).aupdate(
        metadata={
            "last_build": {"ok": False, "error": "Synthetic promotion failed after restoring B"}
        }
    )
    # The failed Cube build rolled catalog updates back: B is still hidden and
    # the active Cube has only A, despite the now-restored source and view.
    assert not await SemanticDataset.objects.filter(
        workspace=setup.workspace, name="source_b__visits", is_visible=True
    ).aexists()
    state = await artifact_data_state(setup.b)
    assert state["queryable"] is False
    assert state["recovery_action"] == "semantic_rebuild"
    recovery = await make_recovery(setup)

    async def rebuild(_workspace_id):
        await restore_b(setup)
        return {"cube_schema": {"ok": True}}

    with (
        patch(
            "apps.workspaces.tasks.rebuild_workspace_semantic_model_core",
            new=AsyncMock(side_effect=rebuild),
        ) as build,
        patch("apps.workspaces.tasks.materialize_workspace_core", new=AsyncMock()) as load,
    ):
        result = await recover_workspace_data.func(task_context(), str(recovery.id))
    assert result["status"] == "completed"
    build.assert_awaited_once()
    load.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tenants", "expected"),
    [
        (
            [
                {
                    "success": False,
                    "state": "not_run",
                    "error": "No usable credential could be resolved",
                    "error_code": ErrorCode.AUTH_CREDENTIAL_MISSING,
                }
            ]
            * 2,
            "open Connected Accounts and connect or reconnect",
        ),
        (
            [
                {
                    "tenant": "source-alpha",
                    "provider": "commcare",
                    "success": False,
                    "error": "Missing credential",
                    "error_code": ErrorCode.AUTH_CREDENTIAL_MISSING,
                },
                {
                    "tenant": "source-beta",
                    "provider": "commcare",
                    "success": False,
                    "error": "Synthetic access denied",
                    "error_code": ErrorCode.AUTH_ACCESS_DENIED,
                },
            ],
            "source-alpha (commcare): no usable sign-in is available",
        ),
        (
            [
                {
                    "success": False,
                    "error": "Synthetic access denied",
                    "error_code": ErrorCode.AUTH_ACCESS_DENIED,
                }
            ],
            "Ask an admin on the affected provider to restore access",
        ),
        (
            [
                {
                    "success": False,
                    "error": f"Synthetic source {index} timed out. " + "details " * 100,
                }
                for index in range(5)
            ],
            "Synthetic source 0 timed out",
        ),
        ([], "Synthetic Cube validation failed"),
        (
            [{"success": True, "error": "An obsolete source error"}],
            "Synthetic Cube validation failed",
        ),
    ],
)
async def test_recovery_failure_prioritizes_source_remedy_over_downstream_cube_error(
    required_setup, tenants, expected
):
    setup = required_setup
    recovery = await make_recovery(setup)
    summary = {
        "all_succeeded": False,
        "tenants": tenants,
        "cube_schema": {"ok": False, "error": "Synthetic Cube validation failed"},
    }
    with patch(
        "apps.workspaces.tasks.materialize_workspace_core", new=AsyncMock(return_value=summary)
    ):
        result = await recover_workspace_data.func(task_context(), str(recovery.id))
    await recovery.arefresh_from_db()
    assert result["status"] == recovery.state == "failed"
    assert recovery.result == summary
    assert expected in recovery.error
    assert len(recovery.error) <= 1000
    state = (await setup.client.get(recovery_url(setup.b))).json()
    assert state["status"] == "failed"
    assert expected in state["detail"]
    if expected != "Synthetic Cube validation failed":
        assert "Synthetic Cube validation failed" not in recovery.error
    if len(tenants) == 2:
        assert recovery.error.count("Connected Accounts") == 1
        if tenants[0].get("tenant") == "source-alpha":
            assert "source-beta (commcare): access was removed upstream" in state["detail"]
            assert "reconnecting alone does not change upstream permissions" in state["detail"]
            assert "Ask an admin on the affected provider" in state["detail"]
    if len(tenants) == 5:
        assert "Synthetic source 2" in recovery.error
        assert "Synthetic source 3" not in recovery.error
