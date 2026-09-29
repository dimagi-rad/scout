"""Characterisation tests for the status rules consolidated in
``apps.workspaces.services.status`` (#251, stage 1).

The DB scenarios pin what the chat prompt and the query surface said *before*
their shared "is a writer touching serving data" rule moved into one helper, so
the move is provably behaviour-preserving.
"""

import pytest

from apps.agents.graph.base import _fetch_semantic_model_context
from apps.semantic.models import CubeSchema, SemanticModel
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.query_state import workspace_query_surface

LOADING = MaterializationRun.RunState.LOADING


async def _ready_semantic_layer(workspace):
    model = await SemanticModel.objects.acreate(
        workspace=workspace, name="Serving", status=SemanticModel.Status.ACTIVE
    )
    await CubeSchema.objects.acreate(
        workspace=workspace,
        semantic_model=model,
        filename="serving.yml",
        content="cubes: []",
        content_hash="x",
        status=CubeSchema.Status.ACTIVE,
    )


async def _second_source(workspace, *, schema_state, run_state=None):
    other = await Tenant.objects.acreate(provider="commcare", external_id="second-source")
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=other)
    schema = await TenantSchema.objects.acreate(
        tenant=other, schema_name="second_source", state=schema_state
    )
    if run_state:
        await MaterializationRun.objects.acreate(
            tenant_schema=schema, pipeline="commcare_sync", state=run_state
        )
    return other


async def _scenario(name, workspace, tenant):
    serving = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="first_source", state=SchemaState.ACTIVE
    )
    if name == "idle":
        return
    if name == "single_blue_green_refresh":
        refresh = await TenantSchema.objects.acreate(
            tenant=tenant, schema_name="first_source_r", state=SchemaState.PROVISIONING
        )
        await MaterializationRun.objects.acreate(
            tenant_schema=refresh, pipeline="commcare_sync", state=LOADING
        )
        return
    if name == "single_in_place_writer":
        await MaterializationRun.objects.acreate(
            tenant_schema=serving, pipeline="commcare_sync", state=LOADING
        )
        return
    other = await _second_source(
        workspace, schema_state=SchemaState.MATERIALIZING, run_state=LOADING
    )
    first = {"tenant_id": str(tenant.id)}
    second = {"tenant_id": str(other.id)}
    coverage = {
        "multi_excluded_source_loading": {
            "included_tenants": [first],
            "excluded_tenants": [second],
        },
        "multi_included_source_loading": {
            "included_tenants": [first, second],
            "excluded_tenants": [],
        },
        "multi_source_both_included_and_excluded": {
            "included_tenants": [first, second],
            "excluded_tenants": [second],
        },
    }[name]
    await WorkspaceViewSchema.objects.acreate(
        workspace=workspace,
        schema_name="serving_view",
        state=SchemaState.ACTIVE,
        tenant_coverage=coverage,
    )


# (scenario, query surface status, prompt lets the agent query previous data)
SCENARIOS = [
    ("idle", "ready", None),
    ("single_blue_green_refresh", "ready", True),
    ("single_in_place_writer", "recovering", False),
    ("multi_excluded_source_loading", "ready", True),
    ("multi_included_source_loading", "recovering", False),
    ("multi_source_both_included_and_excluded", "recovering", False),
]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("scenario", "surface_status", "prompt_serves_previous"), SCENARIOS)
async def test_prompt_and_query_surface_agree_on_serving_writers(
    workspace, tenant, scenario, surface_status, prompt_serves_previous
):
    await _ready_semantic_layer(workspace)
    await _scenario(scenario, workspace, tenant)

    surface = await workspace_query_surface(workspace)
    prompt = await _fetch_semantic_model_context(workspace, write_capable=True)

    assert surface["status"] == surface_status
    assert surface["in_progress"] is (scenario != "idle")
    if prompt_serves_previous is None:
        assert prompt.startswith("Data is loaded and ready")
    elif prompt_serves_previous:
        assert "previously loaded data" in prompt
    else:
        assert "do not call other data tools" in prompt.lower()
