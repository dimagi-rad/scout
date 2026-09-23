"""Recovery selection against owned PostgreSQL publications, not mocked surfaces."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from asgiref.sync import sync_to_async

from apps.artifacts.models import Artifact
from apps.artifacts.tests import test_artifact_view_provenance
from apps.artifacts.tests.test_artifact_view_provenance import (
    _assert_real_count,
    _create_table,
)
from apps.semantic.models import SemanticDataset
from apps.semantic.services.catalog import SemanticCatalogUnavailable, load_physical_tables
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceDataRecovery,
    WorkspaceViewSchema,
)
from apps.workspaces.services.query_state import workspace_query_surface
from apps.workspaces.services.schema_manager import SchemaManager
from apps.workspaces.services.view_sources import ViewSourcesError
from apps.workspaces.tasks import (
    rebuild_workspace_view_schema,
    recover_workspace_data,
    teardown_schema,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]
# Reuse the exact existing isolated PostgreSQL publication fixture.
published_sources = test_artifact_view_provenance.published_sources


def _recovery_url(artifact):
    return f"/api/workspaces/{artifact.workspace_id}/artifacts/{artifact.id}/recovery/"


async def _partial_publication(setup):
    """Expire the required source and really publish only the other tenant."""
    await TenantSchema.objects.filter(pk=setup.schemas[0].pk).aupdate(state=SchemaState.TEARDOWN)
    await teardown_schema.func(str(setup.schemas[0].id))
    result = await rebuild_workspace_view_schema.func(str(setup.workspace.id))
    assert result["cube_schema"]["ok"] is True
    await setup.view.arefresh_from_db()
    await setup.dataset.arefresh_from_db()
    assert setup.dataset.table_name not in setup.view.view_sources["views"]
    assert setup.dataset.metadata["source_tenant_ids"] == [str(setup.tenants[0].id)]
    assert setup.dataset.is_visible is False


async def _fail_view_build(setup):
    # A failed rebuild of an ACTIVE row keeps its last-good views serving (D2), so
    # reach FAILED the way production does: a membership change marks the row
    # PROVISIONING before the rebuild, and that rebuild fails.
    await WorkspaceViewSchema.objects.filter(pk=setup.view.pk).aupdate(
        state=SchemaState.PROVISIONING
    )
    with patch.object(
        SchemaManager, "_create_readonly_role", side_effect=RuntimeError("Synthetic DDL failure")
    ):
        with pytest.raises(RuntimeError, match="Synthetic DDL failure"):
            await sync_to_async(SchemaManager().build_view_schema)(setup.workspace)
    await setup.view.arefresh_from_db()
    assert setup.view.state == SchemaState.FAILED


async def _other_artifact(setup):
    other_dataset = (
        await SemanticDataset.objects.filter(workspace=setup.workspace)
        .exclude(pk=setup.dataset.pk)
        .afirst()
    )
    return await Artifact.objects.acreate(
        workspace=setup.workspace,
        created_by=setup.user,
        title="Synthetic healthy control",
        artifact_type=setup.artifact.artifact_type,
        code="",
        semantic_queries=[{"name": "visits", "measures": [f"{other_dataset.name}.count"]}],
    )


@pytest.mark.parametrize("attribution_error", ["missing", "invalid", "legacy", "metadata"])
async def test_source_repair_preserves_active_load_and_post_does_not_dispatch(
    published_sources, attribution_error
):
    setup = published_sources
    source_map = deepcopy(setup.view.view_sources)
    if attribution_error == "missing":
        del source_map["views"][setup.dataset.table_name]
    elif attribution_error == "invalid":
        source_map["version"] = 99
    elif attribution_error == "legacy":
        source_map = {}
        await SemanticDataset.objects.filter(pk=setup.dataset.pk).aupdate(metadata={})
    else:
        await SemanticDataset.objects.filter(pk=setup.dataset.pk).aupdate(
            metadata={"source_tenant_ids": []}
        )
    await WorkspaceViewSchema.objects.filter(pk=setup.view.pk).aupdate(view_sources=source_map)
    run = await MaterializationRun.objects.acreate(
        tenant_schema=setup.schemas[0], pipeline="synthetic", state="loading"
    )

    # No ThreadJob or WorkspaceDataRecovery exists to hide a classifier error.
    defer = AsyncMock(return_value=SimpleNamespace(id=1701))
    with patch("apps.artifacts.views.recover_workspace_data.defer_async", new=defer):
        status_response = await setup.client.get(_recovery_url(setup.artifact))
        post_response = await setup.client.post(_recovery_url(setup.artifact))
        for response in (post_response, status_response):
            assert response.status_code == 200
            assert response.json()["status"] == "recovering"
            assert response.json()["recovery_action"] is None
            assert response.json()["can_retry"] is False
            assert response.json()["queryable"] is False
        defer.assert_not_awaited()
        assert not await WorkspaceDataRecovery.objects.filter(workspace=setup.workspace).aexists()

        # The missing provenance still needs repair after the existing writer ends.
        await MaterializationRun.objects.filter(pk=run.pk).aupdate(state="completed")
        response = await setup.client.post(_recovery_url(setup.artifact))
    assert response.status_code == 202
    defer.assert_awaited_once()
    recovery = await WorkspaceDataRecovery.objects.aget(workspace=setup.workspace)
    assert recovery.recovery_type == "view_rebuild"


@pytest.mark.parametrize("view_failure", ["ddl", "expired", "no_active_sources"])
async def test_partial_publication_missing_source_dispatches_materialization(
    published_sources, view_failure
):
    setup = published_sources
    await _partial_publication(setup)
    other = await _other_artifact(setup)
    healthy = await setup.client.get(_recovery_url(other))
    assert healthy.json()["status"] == "ready"
    assert healthy.json()["queryable"] is True
    assert healthy.json()["recovery_action"] is None

    if view_failure == "ddl":
        await _fail_view_build(setup)
    elif view_failure == "expired":
        await sync_to_async(SchemaManager().teardown_view_schema)(setup.view)
        await WorkspaceViewSchema.objects.filter(pk=setup.view.pk).aupdate(
            state=SchemaState.EXPIRED
        )
    else:
        await TenantSchema.objects.filter(pk=setup.schemas[1].pk).aupdate(
            state=SchemaState.TEARDOWN
        )
        await teardown_schema.func(str(setup.schemas[1].id))
        with pytest.raises(ValueError, match="no active schema"):
            await sync_to_async(SchemaManager().build_view_schema)(setup.workspace)

    with patch("apps.artifacts.views.run_semantic_query", new=AsyncMock()) as query:
        response = await setup.client.get(setup.url)
    assert response.status_code == 409
    state = response.json()["data_recovery"]
    assert state["tenant_coverage"] is None  # Unavailable views cannot assert serving coverage.
    assert state["recovery_action"] == "materialization"
    query.assert_not_awaited()

    other_state = (await setup.client.get(_recovery_url(other))).json()
    expected = "materialization" if view_failure == "no_active_sources" else "view_rebuild"
    assert other_state["recovery_action"] == expected
    defer = AsyncMock(return_value=SimpleNamespace(id=1702))
    with patch("apps.artifacts.views.recover_workspace_data.defer_async", new=defer):
        response = await setup.client.post(_recovery_url(setup.artifact))
    assert response.status_code == 202
    defer.assert_awaited_once()
    recovery = await WorkspaceDataRecovery.objects.aget(workspace=setup.workspace)
    assert recovery.recovery_type == "materialization"
    assert recovery.source_id == setup.artifact.id


async def test_worker_reassesses_partial_missing_source_under_lock(published_sources):
    setup = published_sources
    await _partial_publication(setup)
    await _fail_view_build(setup)
    recovery = await WorkspaceDataRecovery.objects.acreate(
        workspace=setup.workspace,
        requested_by=setup.user,
        source_type="artifact",
        source_id=setup.artifact.id,
        recovery_type="view_rebuild",  # Queued before the source disappeared.
    )
    real_rebuild = rebuild_workspace_view_schema.func

    async def restore_source(workspace_id, user_id, job_id):
        assert workspace_id == str(setup.workspace.id)
        assert user_id == str(setup.user.id)
        assert job_id == 1703
        restored = await sync_to_async(SchemaManager().provision)(setup.tenants[0])
        await sync_to_async(_create_table)(setup.dsn, restored.schema_name, count=3)
        return await real_rebuild(workspace_id)

    with (
        patch(
            "apps.workspaces.tasks.materialize_workspace_core",
            new=AsyncMock(side_effect=restore_source),
        ) as materialize,
        patch(
            "apps.workspaces.tasks.rebuild_workspace_view_schema.func",
            new=AsyncMock(return_value={"error": "Synthetic view-only attempt"}),
        ) as view,
    ):
        result = await recover_workspace_data.func(
            SimpleNamespace(job=SimpleNamespace(id=1703)), str(recovery.id)
        )
    materialize.assert_awaited_once()
    view.assert_not_awaited()
    assert result["status"] == "completed"
    await recovery.arefresh_from_db()
    assert recovery.state == WorkspaceDataRecovery.State.COMPLETED
    await _assert_real_count(setup, 3)


@pytest.mark.parametrize(
    "invalid_evidence",
    ["no_coverage", "malformed", "overlap", "foreign", "incomplete", "included", "duplicate"],
)
@pytest.mark.parametrize("failed_view", [False, True])
async def test_missing_entry_requires_scoped_exclusion_evidence(
    published_sources, invalid_evidence, failed_view
):
    setup = published_sources
    await _partial_publication(setup)
    if failed_view:
        await _fail_view_build(setup)
    coverage = deepcopy(setup.view.tenant_coverage)
    if invalid_evidence == "no_coverage":
        coverage = {}
    elif invalid_evidence == "malformed":
        coverage["excluded_tenants"] = ["invalid"]
    elif invalid_evidence == "overlap":
        coverage["included_tenants"].extend(coverage["excluded_tenants"])
    elif invalid_evidence == "foreign":
        coverage["excluded_tenants"].append({"tenant_id": str(uuid4())})
    elif invalid_evidence == "incomplete":
        coverage["included_tenants"] = []
    elif invalid_evidence == "duplicate":
        coverage["excluded_tenants"] *= 2
    else:
        coverage["included_tenants"].extend(coverage["excluded_tenants"])
        coverage["excluded_tenants"] = []
    await WorkspaceViewSchema.objects.filter(pk=setup.view.pk).aupdate(tenant_coverage=coverage)
    state = (await setup.client.get(_recovery_url(setup.artifact))).json()
    assert state["queryable"] is False
    assert state["recovery_action"] == "view_rebuild"


@pytest.mark.parametrize("invalid_owner", ["unknown", "empty", "foreign", "mismatch", "ambiguous"])
async def test_partial_publication_does_not_guess_owner_when_views_are_unavailable(
    published_sources, invalid_owner
):
    setup = published_sources
    await _partial_publication(setup)
    await _fail_view_build(setup)
    metadata = {}
    if invalid_owner == "empty":
        metadata = {"source_tenant_ids": []}
    elif invalid_owner == "foreign":
        metadata = {"source_tenant_ids": [str(uuid4())]}
    elif invalid_owner == "ambiguous":
        metadata = {"source_tenant_ids": [str(tenant.id) for tenant in setup.tenants]}
        await TenantSchema.objects.filter(pk=setup.schemas[1].pk).aupdate(state=SchemaState.EXPIRED)
        await WorkspaceViewSchema.objects.filter(pk=setup.view.pk).aupdate(
            tenant_coverage={
                "included_tenants": [],
                "excluded_tenants": [
                    SchemaManager._tenant_coverage_entry(tenant) for tenant in setup.tenants
                ],
            }
        )
    elif invalid_owner == "mismatch":
        metadata = {"source_tenant_ids": [str(setup.tenants[0].id)]}
        source_map = deepcopy(setup.view.view_sources)
        source_map["views"][setup.dataset.table_name] = {
            "tenant_id": str(setup.tenants[1].id),
            "source_table_name": "raw_visits",
        }
        await WorkspaceViewSchema.objects.filter(pk=setup.view.pk).aupdate(view_sources=source_map)
    await SemanticDataset.objects.filter(pk=setup.dataset.pk).aupdate(metadata=metadata)
    state = (await setup.client.get(_recovery_url(setup.artifact))).json()
    assert state["queryable"] is False
    assert state["recovery_action"] == "view_rebuild"


async def test_loaded_excluded_source_needs_only_view_rebuild(published_sources):
    setup = published_sources
    await _partial_publication(setup)
    await _fail_view_build(setup)
    restored = await sync_to_async(SchemaManager().provision)(setup.tenants[0])
    await sync_to_async(_create_table)(setup.dsn, restored.schema_name)
    state = (await setup.client.get(_recovery_url(setup.artifact))).json()
    assert state["queryable"] is False
    assert state["recovery_action"] == "view_rebuild"


@pytest.mark.parametrize("source_restored", [False, True])
async def test_older_ready_surface_cannot_hide_current_explicit_missing_view(
    published_sources, source_restored
):
    setup = published_sources
    full_surface = await workspace_query_surface(setup.workspace)
    assert full_surface["queryable"] is True
    await TenantSchema.objects.filter(pk=setup.schemas[0].pk).aupdate(state=SchemaState.TEARDOWN)
    await teardown_schema.func(str(setup.schemas[0].id))
    # Physical publication precedes the catalog/Cube phase. The old model is
    # still readable while the source is restored before its new view is built.
    partial_view = await sync_to_async(SchemaManager().build_view_schema)(setup.workspace)
    assert setup.dataset.table_name not in partial_view.view_sources["views"]
    if source_restored:
        restored = await sync_to_async(SchemaManager().provision)(setup.tenants[0])
        await sync_to_async(_create_table)(setup.dsn, restored.schema_name, count=3)
    expected = "view_rebuild" if source_restored else "materialization"

    defer = AsyncMock(return_value=SimpleNamespace(id=1704))
    with (
        patch(
            "apps.artifacts.services.query_state.workspace_query_surface",
            new=AsyncMock(return_value=full_surface),
        ),
        patch("apps.artifacts.views.recover_workspace_data.defer_async", new=defer),
        patch("apps.artifacts.views.run_semantic_query", new=AsyncMock()) as query,
    ):
        state = (await setup.client.get(_recovery_url(setup.artifact))).json()
        assert state["queryable"] is False
        assert state["recovery_action"] == expected
        assert (await setup.client.get(setup.url)).status_code == 409
        query.assert_not_awaited()
        response = await setup.client.post(_recovery_url(setup.artifact))
    assert response.status_code == 202
    defer.assert_awaited_once()
    recovery = await WorkspaceDataRecovery.objects.aget(workspace=setup.workspace)
    assert recovery.recovery_type == expected


@pytest.mark.parametrize("invalid_map", ["missing_entry", "foreign_owner", "invalid_version"])
async def test_catalog_source_errors_preserve_query_layer_repair_guidance(
    published_sources, invalid_map
):
    setup = published_sources
    source_map = deepcopy(setup.view.view_sources)
    foreign_id = str(uuid4())
    if invalid_map == "missing_entry":
        del source_map["views"][setup.dataset.table_name]
    elif invalid_map == "foreign_owner":
        source_map["views"][setup.dataset.table_name]["tenant_id"] = foreign_id
    else:
        source_map["version"] = 99
    await WorkspaceViewSchema.objects.filter(pk=setup.view.pk).aupdate(view_sources=source_map)
    with pytest.raises(SemanticCatalogUnavailable) as error:
        await sync_to_async(load_physical_tables)(setup.workspace)
    assert error.value.schema_status == "failed"
    assert str(error.value) == (
        "The workspace view source map is invalid. Rebuild the query layer."
    )
    assert "refresh workspace data" not in str(error.value).lower()
    assert foreign_id not in str(error.value)
    assert setup.dataset.table_name not in str(error.value)
    assert isinstance(error.value.__cause__, ViewSourcesError)


async def test_unrelated_catalog_error_keeps_generic_private_message(published_sources):
    setup = published_sources
    with (
        patch(
            "apps.semantic.services.catalog._load_physical_tables_async",
            new=AsyncMock(side_effect=RuntimeError("Synthetic private connection detail")),
        ),
        pytest.raises(SemanticCatalogUnavailable) as error,
    ):
        await sync_to_async(load_physical_tables)(setup.workspace)
    assert error.value.schema_status == "unavailable"
    assert str(error.value) == "Data unavailable. Please refresh workspace data."
