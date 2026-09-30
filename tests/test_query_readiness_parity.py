"""Saved artifacts and unsaved queries share one readiness verdict over real catalog rows.

Only a saved artifact's own failed repair history may change its verdict; everything
else about the query surface must match between the two callers.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.artifacts.models import Artifact, ArtifactType
from apps.artifacts.services.query_state import artifact_query_surface
from apps.semantic.models import CubeSchema, SemanticModel
from apps.semantic.services.catalog import PhysicalTable, ensure_semantic_model
from apps.semantic.services.cube import cube_schema_yaml, generate_cube_schema
from apps.semantic.services.query_outcomes import QueryReadiness, query_readiness_error
from apps.users.models import Tenant, User
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceDataRecovery,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.schema_manager import SchemaManager

pytestmark = [pytest.mark.django_db(transaction=True)]

QUERY_A = {"measures": ["source_a__visits.count"]}
QUERY_B = {"measures": ["source_b__visits.count"]}


@pytest.fixture
def surface_setup():
    """Source A is loaded and published; source B expired and was dropped from the view."""
    workspace = Workspace.objects.create(name="Readiness parity")
    user = User.objects.create_user(email="readiness-parity@example.com", password="test-only")
    tenants = [
        Tenant.objects.create(
            provider="commcare", external_id=f"parity-{key}", canonical_name=f"Source-{key}"
        )
        for key in ("A", "B")
    ]
    for tenant in tenants:
        WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)
    schema_a = TenantSchema.objects.create(
        tenant=tenants[0], schema_name="parity_source_a", state=SchemaState.ACTIVE
    )
    schema_b = TenantSchema.objects.create(
        tenant=tenants[1], schema_name="parity_source_b", state=SchemaState.EXPIRED
    )
    view = WorkspaceViewSchema.objects.create(
        workspace=workspace,
        schema_name="ws_readiness_parity",
        state=SchemaState.ACTIVE,
        tenant_coverage={
            "included_tenants": [SchemaManager._tenant_coverage_entry(tenants[0])],
            "excluded_tenants": [SchemaManager._tenant_coverage_entry(tenants[1])],
        },
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
    for loaded in (tables, tables[:1]):
        with patch(
            "apps.semantic.services.catalog.load_physical_tables",
            return_value=(view.schema_name, loaded),
        ):
            model = ensure_semantic_model(workspace)
    model.metadata = {"last_build": {"ok": True, "at": timezone.now().isoformat()}}
    model.save(update_fields=["metadata"])
    cube = CubeSchema.objects.create(
        workspace=workspace,
        semantic_model=model,
        filename="parity.yaml",
        content=cube_schema_yaml(generate_cube_schema(model)),
        content_hash="parity",
        status=CubeSchema.Status.ACTIVE,
    )
    return SimpleNamespace(
        workspace=workspace,
        user=user,
        schema_a=schema_a,
        schema_b=schema_b,
        model=model,
        cube=cube,
    )


async def _no_change(setup):
    pass


async def _delete_model(setup):
    await SemanticModel.objects.filter(id=setup.model.id).adelete()


async def _draft_model(setup):
    await SemanticModel.objects.filter(id=setup.model.id).aupdate(status=SemanticModel.Status.DRAFT)


async def _delete_cube(setup):
    await CubeSchema.objects.filter(id=setup.cube.id).adelete()


async def _fail_cube_build(setup):
    await SemanticModel.objects.filter(id=setup.model.id).aupdate(
        metadata={"last_build": {"ok": False, "error": "Cube build failed"}}
    )


async def _unpromote_members(setup):
    await CubeSchema.objects.filter(id=setup.cube.id).aupdate(content="cubes: []")


async def _load_source_b(setup):
    await TenantSchema.objects.filter(id=setup.schema_b.id).aupdate(state=SchemaState.ACTIVE)


async def _refresh_source_a(setup):
    await MaterializationRun.objects.acreate(tenant_schema=setup.schema_a, pipeline="parity")


READINESS_CASES = {
    "ready": (_no_change, QUERY_A, "ready", True, None),
    "model_missing": (_delete_model, QUERY_A, "needs_semantic_rebuild", False, "semantic_rebuild"),
    "model_draft": (_draft_model, QUERY_A, "model_drift", False, None),
    "cube_schema_missing": (
        _delete_cube,
        QUERY_A,
        "needs_semantic_rebuild",
        False,
        "semantic_rebuild",
    ),
    "cube_build_failed": (_fail_cube_build, QUERY_A, "ready", True, "semantic_rebuild"),
    "member_not_promoted": (
        _unpromote_members,
        QUERY_A,
        "needs_semantic_rebuild",
        False,
        "semantic_rebuild",
    ),
    "member_missing": (
        _no_change,
        {"measures": ["source_a__visits.deleted"]},
        "model_drift",
        False,
        None,
    ),
    "member_invalid": (_no_change, {"measures": ["count"]}, "model_drift", False, None),
    "dataset_missing": (_no_change, {"measures": ["absent.count"]}, "model_drift", False, None),
    "data_not_loaded": (_no_change, QUERY_B, "needs_materialization", False, "materialization"),
    "view_unpublished": (_load_source_b, QUERY_B, "needs_view_rebuild", False, "view_rebuild"),
    "source_refreshing": (_refresh_source_a, QUERY_A, "recovering", False, None),
}


async def _saved_artifact(setup, query):
    return await Artifact.objects.acreate(
        workspace=setup.workspace,
        created_by=setup.user,
        title="Parity",
        artifact_type=ArtifactType.STORY,
        code="",
        semantic_queries=[{"name": "q", **query}],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(READINESS_CASES))
async def test_saved_and_unsaved_queries_share_readiness(surface_setup, case):
    change, query, status, queryable, action = READINESS_CASES[case]
    await change(surface_setup)
    artifact = await _saved_artifact(surface_setup, query)

    saved = await artifact_query_surface(artifact)
    unsaved = await QueryReadiness(surface_setup.workspace, [query]).surface()

    assert saved == unsaved
    assert (saved["status"], saved["queryable"], saved["recovery_action"]) == (
        status,
        queryable,
        action,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(READINESS_CASES))
async def test_unsaved_query_error_follows_the_shared_verdict(surface_setup, case):
    change, query, _status, queryable, action = READINESS_CASES[case]
    await change(surface_setup)

    result = await query_readiness_error(
        surface_setup.workspace, query, "VALIDATION_ERROR", "Failed", category="invalid_query"
    )

    if queryable:
        assert result["error"]["category"] == "invalid_query"
        assert result["error"]["recovery_action"] is None
    else:
        assert result["error"]["category"] == "data_unavailable"
        assert result["error"]["recovery_action"] == action
    assert result["error"]["message"] == "Failed"


@pytest.mark.asyncio
async def test_only_a_saved_artifact_carries_its_failed_repair_history(surface_setup):
    artifact = await _saved_artifact(surface_setup, QUERY_A)
    await WorkspaceDataRecovery.objects.acreate(
        workspace=surface_setup.workspace,
        requested_by=surface_setup.user,
        source_type="artifact",
        source_id=artifact.id,
        recovery_type="view_rebuild",
        state=WorkspaceDataRecovery.State.FAILED,
        error="Rebuild failed",
    )

    saved = await artifact_query_surface(artifact)
    unsaved = await QueryReadiness(surface_setup.workspace, [QUERY_A]).surface()

    assert unsaved["recovery_action"] is None
    assert saved == {
        **unsaved,
        "recovery_action": "view_rebuild",
        "message": "Showing the last available data. The latest repair did not complete.",
        "detail": "Rebuild failed",
    }


@pytest.mark.asyncio
async def test_a_later_verified_build_clears_the_failed_repair_overlay(surface_setup):
    artifact = await _saved_artifact(surface_setup, QUERY_A)
    await WorkspaceDataRecovery.objects.acreate(
        workspace=surface_setup.workspace,
        requested_by=surface_setup.user,
        source_type="artifact",
        source_id=artifact.id,
        recovery_type="view_rebuild",
        state=WorkspaceDataRecovery.State.FAILED,
        error="Rebuild failed",
    )
    await SemanticModel.objects.filter(id=surface_setup.model.id).aupdate(
        metadata={"last_build": {"ok": True, "at": timezone.now().isoformat()}}
    )

    saved = await artifact_query_surface(artifact)
    unsaved = await QueryReadiness(surface_setup.workspace, [QUERY_A]).surface()

    assert saved == unsaved
