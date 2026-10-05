"""Characterisation tests for the status rules consolidated in
``apps.workspaces.services.status`` (#251, stage 1).

The DB scenarios pin what the chat prompt and the query surface said *before*
their shared "is a writer touching serving data" rule moved into one helper, so
the move is provably behaviour-preserving.
"""

import pytest

from apps.agents.graph.prompt_context import _fetch_semantic_model_context
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
from apps.workspaces.services.status import (
    derive_schema_status,
    serving_excluded_tenant_ids,
    workspace_schema_status,
)

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
    if name == "single_first_load":
        first = await TenantSchema.objects.acreate(
            tenant=tenant, schema_name="first_source", state=SchemaState.PROVISIONING
        )
        await MaterializationRun.objects.acreate(
            tenant_schema=first, pipeline="commcare_sync", state=LOADING
        )
        return
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
        workspace, schema_state=SchemaState.PROVISIONING, run_state=LOADING
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
    ("single_first_load", "recovering", False),
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


@pytest.mark.parametrize(
    ("tenant_count", "active_count", "load_running", "view_state", "expected"),
    [
        (0, 0, False, None, "not_loaded"),
        (1, 1, False, None, "available"),
        (1, 1, True, None, "available"),
        (1, 0, True, None, "provisioning"),
        (1, 0, False, None, "not_loaded"),
        (2, 2, False, None, "not_loaded"),
        (2, 2, True, None, "provisioning"),
        (2, 0, False, SchemaState.ACTIVE, "available"),
        (2, 0, True, SchemaState.ACTIVE, "available"),
        (2, 2, False, SchemaState.FAILED, "failed"),
        (2, 2, True, SchemaState.FAILED, "provisioning"),
        (2, 2, False, SchemaState.PROVISIONING, "not_loaded"),
        (2, 2, True, SchemaState.PROVISIONING, "provisioning"),
        (2, 2, False, SchemaState.EXPIRED, "not_loaded"),
    ],
)
def test_derive_schema_status(tenant_count, active_count, load_running, view_state, expected):
    assert derive_schema_status(tenant_count, active_count, load_running, view_state) == expected


@pytest.mark.parametrize(
    ("coverage", "expected"),
    [
        (None, set()),
        ({"included_tenants": [], "excluded_tenants": []}, set()),
        (
            {"included_tenants": [{"tenant_id": "a"}], "excluded_tenants": [{"tenant_id": "b"}]},
            {"b"},
        ),
        (
            {
                "included_tenants": [{"tenant_id": "a"}, {"tenant_id": "b"}],
                "excluded_tenants": [{"tenant_id": "b"}],
            },
            set(),
        ),
    ],
)
def test_serving_excluded_tenant_ids(coverage, expected):
    assert serving_excluded_tenant_ids(coverage) == expected


def test_workspace_schema_status_counts_sources_not_schema_rows():
    active = {"a"}
    assert workspace_schema_status(["a"], active, False, None) == "available"
    assert workspace_schema_status(["b"], active, True, None) == "provisioning"
    assert workspace_schema_status(["c"], active, False, None) == "not_loaded"
    assert workspace_schema_status([], active, False, None) == "not_loaded"
