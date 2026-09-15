"""Tests for schema context injection into the agent system prompt."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.utils import timezone

from apps.agents.graph import base as graph_base
from apps.agents.graph.base import (
    _fetch_multi_tenant_schema_context,
    _fetch_schema_context,
    _fetch_semantic_model_context,
)
from apps.semantic.models import SemanticModel
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceTenant,
    WorkspaceViewSchema,
)


@pytest.fixture
def mock_tenant():
    m = MagicMock()
    m.external_id = "test-domain"
    m.canonical_name = "Test Domain"
    m.provider = "commcare"
    return m


@pytest.fixture
def mock_user():
    return MagicMock()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.parametrize("state", sorted(MaterializationRun.ACTIVE_STATES))
async def test_semantic_context_active_run_takes_precedence_over_active_model(
    workspace, tenant, interactive, state
):
    """A last-known-good model must not hide a materialization in flight."""
    schema = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name="test_domain_active",
        state=SchemaState.ACTIVE,
    )
    await SemanticModel.objects.acreate(
        workspace=workspace,
        name="Existing semantic model",
        status=SemanticModel.Status.ACTIVE,
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=state,
    )

    result = await _fetch_semantic_model_context(workspace, interactive=interactive)

    assert "in progress" in result.lower()
    if interactive:
        assert "trigger another" in result.lower()
        assert "resume" not in result.lower()
        assert "if this conversation" not in result.lower()
        assert "do not promise an automatic follow-up" in result.lower()
    else:
        assert "waits" in result.lower()
    assert "Data is loaded and ready" not in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.parametrize("has_serving_schema", [True, False])
@pytest.mark.parametrize("has_catalog", [True, False])
async def test_refresh_keeps_previous_data_queryable_only_when_ready(
    workspace, tenant, interactive, has_serving_schema, has_catalog
):
    if has_serving_schema:
        await TenantSchema.objects.acreate(
            tenant=tenant, schema_name="previous_data", state=SchemaState.ACTIVE
        )
    if has_catalog:
        await SemanticModel.objects.acreate(
            workspace=workspace, name="Previous model", status=SemanticModel.Status.ACTIVE
        )
    refresh_schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="refresh_target", state=SchemaState.PROVISIONING
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=refresh_schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
    )

    result = await _fetch_semantic_model_context(workspace, interactive=interactive)

    assert "in progress" in result.lower()
    if has_serving_schema and has_catalog:
        assert "previously loaded data" in result.lower()
        assert "do not trigger another" in result.lower()
        assert "do not call other data tools" not in result.lower()
        assert "semantic_query" in result
        assert "do not promise an automatic follow-up" in result.lower()
    else:
        assert "previously loaded data" not in result.lower()
        assert "Data is loaded and ready" not in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("has_view", [True, False])
@pytest.mark.parametrize("in_place_run", [True, False])
async def test_multi_source_refresh_requires_serving_view_and_no_in_place_writer(
    workspace, tenant, has_view, in_place_run
):
    await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="refresh_previous", state=SchemaState.ACTIVE
    )
    refresh = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="refresh_new", state=SchemaState.PROVISIONING
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=refresh, pipeline="commcare_sync", state=MaterializationRun.RunState.LOADING
    )
    other_tenant = await Tenant.objects.acreate(provider="commcare", external_id="other-refresh")
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=other_tenant)
    other_schema = await TenantSchema.objects.acreate(
        tenant=other_tenant, schema_name="other_serving", state=SchemaState.ACTIVE
    )
    if in_place_run:
        await MaterializationRun.objects.acreate(
            tenant_schema=other_schema,
            pipeline="commcare_sync",
            state=MaterializationRun.RunState.LOADING,
        )
    await SemanticModel.objects.acreate(
        workspace=workspace, name="Previous model", status=SemanticModel.Status.ACTIVE
    )
    if has_view:
        await WorkspaceViewSchema.objects.acreate(
            workspace=workspace, schema_name="serving_view", state=SchemaState.ACTIVE
        )

    result = await _fetch_semantic_model_context(workspace)

    assert "in progress" in result.lower()
    if has_view and not in_place_run:
        assert "previously loaded data" in result.lower()
    else:
        assert "do not call other data tools" in result.lower()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_old_untracked_active_run_does_not_claim_data_ready(workspace, tenant):
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="long_running_load", state=SchemaState.ACTIVE
    )
    await SemanticModel.objects.acreate(
        workspace=workspace, name="Previous model", status=SemanticModel.Status.ACTIVE
    )
    run = await MaterializationRun.objects.acreate(
        tenant_schema=schema, pipeline="commcare_sync", state=MaterializationRun.RunState.LOADING
    )
    await MaterializationRun.objects.filter(pk=run.pk).aupdate(
        started_at=timezone.now() - timedelta(hours=2)
    )

    result = await _fetch_semantic_model_context(workspace)

    assert "do not call other data tools" in result.lower()
    assert "Data is loaded and ready" not in result
    await run.arefresh_from_db()
    assert run.state == MaterializationRun.RunState.LOADING
    assert run.procrastinate_job_id is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_semantic_context_completed_run_does_not_hide_active_model(workspace, tenant):
    """A terminal run is historical and must not replace ready-model guidance."""
    schema = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name="test_domain_completed",
        state=SchemaState.ACTIVE,
    )
    await SemanticModel.objects.acreate(
        workspace=workspace,
        name="Existing semantic model",
        status=SemanticModel.Status.ACTIVE,
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
    )

    result = await _fetch_semantic_model_context(workspace)

    assert "Data is loaded and ready" in result
    assert "in progress" not in result.lower()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("interactive", [True, False])
async def test_prompt_availability_changes_within_cache_ttl(workspace, tenant, user, interactive):
    graph_base._system_prompt_cache.clear()
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="prompt_transition", state=SchemaState.ACTIVE
    )
    await SemanticModel.objects.acreate(
        workspace=workspace, name="Existing model", status=SemanticModel.Status.ACTIVE
    )
    with (
        patch.object(graph_base, "time", MagicMock(monotonic=MagicMock(return_value=100))),
        patch("apps.agents.graph.base.KnowledgeRetriever") as retriever,
    ):
        retriever.return_value.retrieve = AsyncMock(return_value="Knowledge")
        stable, ready = await graph_base._build_system_prompt(workspace, user, interactive)
        run = await MaterializationRun.objects.acreate(
            tenant_schema=schema,
            pipeline="commcare_sync",
            state=MaterializationRun.RunState.LOADING,
        )
        stable_loading, loading = await graph_base._build_system_prompt(
            workspace, user, interactive
        )
        await MaterializationRun.objects.filter(pk=run.pk).aupdate(
            state=MaterializationRun.RunState.COMPLETED
        )
        stable_done, done = await graph_base._build_system_prompt(workspace, user, interactive)

    assert "Data is loaded and ready" in ready
    assert "in progress" in loading.lower()
    assert "Data is loaded and ready" not in loading
    assert "Data is loaded and ready" in done
    assert "in progress" not in done.lower()
    assert stable == stable_loading == stable_done
    retriever.return_value.retrieve.assert_awaited_once()
    graph_base._system_prompt_cache.clear()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_schema_context_not_provisioned(mock_tenant, mock_user):
    """Returns 'no data' block when TenantSchema does not exist."""
    with patch("apps.agents.graph.base.TenantSchema") as MockTS:
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=None)
        result = await _fetch_schema_context(mock_tenant, mock_user)

    assert "No data has been loaded yet" in result
    assert "run_materialization" in result
    # Finding 02#6: the prompt must NOT instruct a `pipeline=` argument — the
    # run_materialization MCP tool has no pipeline parameter (routing moved into
    # materialize_workspace per-provider) and its LLM-facing schema is empty.
    # The multi-tenant branch already omits it; the single-tenant branch must too.
    assert "pipeline=" not in result
    assert 'pipeline="' not in result


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_schema_context_materializing(mock_tenant, mock_user):
    """Returns 'currently loading' block when schema state is materializing."""
    from apps.workspaces.models import SchemaState

    mock_ts = MagicMock()
    mock_ts.state = SchemaState.MATERIALIZING

    with patch("apps.agents.graph.base.TenantSchema") as MockTS:
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
        result = await _fetch_schema_context(mock_tenant, mock_user)

    assert "loading" in result.lower()
    assert "run_materialization" not in result
    assert "resume" not in result.lower()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_schema_context_active_compact(mock_tenant, mock_user):
    """Active schema guidance does not embed table names in the prompt."""
    from apps.workspaces.models import SchemaState

    mock_ts = MagicMock()
    mock_ts.state = SchemaState.ACTIVE

    mock_tables = [
        {
            "name": "cases",
            "description": "CommCare cases",
            "materialized_row_count": 1000,
            "row_count_verified": False,
            "materialized_at": "2026-03-02T10:00:00",
        },
        {
            "name": "forms",
            "description": "CommCare forms",
            "materialized_row_count": 500,
            "row_count_verified": False,
            "materialized_at": "2026-03-02T10:00:00",
        },
    ]

    with (
        patch("apps.agents.graph.base.TenantSchema") as MockTS,
        patch(
            "apps.agents.graph.base.pipeline_list_tables",
            new=AsyncMock(return_value=mock_tables),
        ),
        patch(
            "apps.transformations.services.lineage.aget_terminal_assets",
            new=AsyncMock(return_value=[]),
        ),
    ):
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)

        result = await _fetch_schema_context(mock_tenant, mock_user)

    assert "Data is loaded and ready" in result
    assert "2026-03-02T10:00:00" in result
    assert "list_datasets" in result
    assert "describe_dataset" in result
    assert "cases" not in result
    assert "forms" not in result
    assert "1,000" not in result and "1000" not in result


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_schema_context_active_full(mock_tenant, mock_user):
    """Active schema guidance does not embed column details."""
    from apps.workspaces.models import SchemaState

    mock_ts = MagicMock()
    mock_ts.state = SchemaState.ACTIVE

    mock_tables = [
        {
            "name": "cases",
            "description": "CommCare cases",
            "materialized_row_count": 100,
            "row_count_verified": False,
            "materialized_at": "2026-03-02T10:00:00",
        },
    ]

    with (
        patch("apps.agents.graph.base.TenantSchema") as MockTS,
        patch(
            "apps.agents.graph.base.pipeline_list_tables",
            new=AsyncMock(return_value=mock_tables),
        ),
        patch(
            "apps.transformations.services.lineage.aget_terminal_assets",
            new=AsyncMock(return_value=[]),
        ),
    ):
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)

        result = await _fetch_schema_context(mock_tenant, mock_user)

    assert "Data is loaded and ready" in result
    assert "list_datasets" in result
    assert "case_id" not in result
    assert "cases" not in result


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_schema_context_no_get_schema_status_instruction(mock_tenant, mock_user):
    """The returned text must NOT instruct the agent to call get_schema_status."""
    from apps.workspaces.models import SchemaState

    mock_ts = MagicMock()
    mock_ts.state = SchemaState.ACTIVE

    with (
        patch("apps.agents.graph.base.TenantSchema") as MockTS,
        patch(
            "apps.agents.graph.base.pipeline_list_tables",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "apps.transformations.services.lineage.aget_terminal_assets",
            new=AsyncMock(return_value=[]),
        ),
    ):
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)

        result = await _fetch_schema_context(mock_tenant, mock_user)

    assert "call `get_schema_status`" not in result
    assert "start of every conversation" not in result


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_build_system_prompt_no_schema_status_call():
    """The assembled system prompt must not instruct the agent to call get_schema_status."""
    from apps.agents.graph.base import _build_system_prompt

    mock_workspace = MagicMock()
    mock_workspace.system_prompt = None
    mock_workspace.tenants.acount = AsyncMock(return_value=1)
    mock_workspace.tenants.aexists = AsyncMock(return_value=True)

    with (
        patch("apps.agents.graph.base.KnowledgeRetriever") as MockKR,
        patch(
            "apps.agents.graph.base._fetch_semantic_model_context",
            new=AsyncMock(return_value="Data is loaded. Use `list_datasets`."),
        ),
    ):
        MockKR.return_value.retrieve = AsyncMock(return_value="")

        # _build_system_prompt returns a (stable, volatile) split (arch #254).
        prompt = "\n".join(await _build_system_prompt(mock_workspace, MagicMock()))

    assert "call `get_schema_status`" not in prompt
    assert "start of every conversation" not in prompt
    assert "## Data Availability" in prompt
    assert "list_datasets" in prompt


# --- Multi-tenant ---


class _AsyncIter:
    """Re-iterable async iterator over a fixed list, for mocking Django async QuerySets."""

    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        # Return a fresh iterator so `async for` can be used multiple times.
        return _AsyncIter(self._items).__aiter_inner__()

    async def __aiter_inner__(self):
        for item in self._items:
            yield item


@pytest.fixture
def mock_multi_workspace():
    """Workspace with 2 tenants. Subclass tunes `tenants` queryset behavior per test."""
    ws = MagicMock()
    ws.id = "11111111-1111-1111-1111-111111111111"
    t1 = MagicMock()
    t1.id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    t2 = MagicMock()
    t2.id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    ws.tenants.all.return_value = _AsyncIter([t1, t2])
    return ws


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_multi_tenant_no_view_schema_no_runs(mock_multi_workspace, mock_user):
    """No view schema and no active runs -> agent told to call run_materialization."""
    with (
        patch("apps.agents.graph.base.WorkspaceViewSchema") as MockVS,
        patch("apps.agents.graph.base.MaterializationRun") as MockMR,
    ):
        MockVS.objects.filter.return_value.afirst = AsyncMock(return_value=None)
        MockMR.objects.filter.return_value.afirst = AsyncMock(return_value=None)
        MockMR.ACTIVE_STATES = frozenset({"started", "discovering", "loading", "transforming"})

        result = await _fetch_multi_tenant_schema_context(mock_multi_workspace, mock_user)

    assert "No data has been loaded yet" in result
    assert "run_materialization" in result
    assert "multi-tenant" in result.lower()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_multi_tenant_view_schema_materializing(mock_multi_workspace, mock_user):
    """View schema in MATERIALIZING -> 'still loading' message."""
    from apps.workspaces.models import SchemaState

    vs = MagicMock()
    vs.state = SchemaState.MATERIALIZING

    with (
        patch("apps.agents.graph.base.WorkspaceViewSchema") as MockVS,
        patch("apps.agents.graph.base.MaterializationRun") as MockMR,
    ):
        MockVS.objects.filter.return_value.afirst = AsyncMock(return_value=vs)
        MockMR.objects.filter.return_value.afirst = AsyncMock(return_value=None)
        MockMR.ACTIVE_STATES = frozenset({"started", "discovering", "loading", "transforming"})

        result = await _fetch_multi_tenant_schema_context(mock_multi_workspace, mock_user)

    assert "in progress" in result.lower()
    assert "run_materialization" not in result
    assert "resume" not in result.lower()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_multi_tenant_active_materialization_run(mock_multi_workspace, mock_user):
    """An active MaterializationRun for a tenant -> 'still loading' message, even if
    the view schema is not present yet."""
    active_run = MagicMock()

    with (
        patch("apps.agents.graph.base.WorkspaceViewSchema") as MockVS,
        patch("apps.agents.graph.base.MaterializationRun") as MockMR,
    ):
        MockVS.objects.filter.return_value.afirst = AsyncMock(return_value=None)
        MockMR.objects.filter.return_value.afirst = AsyncMock(return_value=active_run)
        MockMR.ACTIVE_STATES = frozenset({"started", "discovering", "loading", "transforming"})

        result = await _fetch_multi_tenant_schema_context(mock_multi_workspace, mock_user)

    assert "in progress" in result.lower()
    assert "run_materialization" not in result
    assert "resume" not in result.lower()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_multi_tenant_active_with_tables(mock_multi_workspace, mock_user):
    """View schema ACTIVE -> emits status and discovery-tool guidance, not view names."""
    from apps.workspaces.models import SchemaState

    vs = MagicMock()
    vs.state = SchemaState.ACTIVE

    completed_run = MagicMock()
    completed_run.completed_at.isoformat.return_value = "2026-05-22T10:00:00"

    with (
        patch("apps.agents.graph.base.WorkspaceViewSchema") as MockVS,
        patch("apps.agents.graph.base.MaterializationRun") as MockMR,
    ):
        MockVS.objects.filter.return_value.afirst = AsyncMock(return_value=vs)
        MockMR.ACTIVE_STATES = frozenset({"started", "discovering", "loading", "transforming"})
        # filter(...).afirst() resolves the active-run check (None);
        # filter(...).order_by(...).afirst() resolves the last-completed-run lookup.
        MockMR.objects.filter.return_value.afirst = AsyncMock(return_value=None)
        MockMR.objects.filter.return_value.order_by.return_value.afirst = AsyncMock(
            return_value=completed_run
        )

        result = await _fetch_multi_tenant_schema_context(mock_multi_workspace, mock_user)

    assert "Data is loaded and ready" in result
    assert "2026-05-22T10:00:00" in result
    assert "tenant_a__raw_cases" not in result
    assert "tenant_b__raw_forms" not in result
    assert "list_datasets" in result
    assert "describe_dataset" in result


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_build_system_prompt_multi_tenant_no_data_pre_fetched():
    """Multi-tenant workspace with no data: system prompt tells the agent upfront,
    NOT 'call list_tables to discover'."""
    from apps.agents.graph.base import _build_system_prompt

    ws = MagicMock()
    ws.id = "22222222-2222-2222-2222-222222222222"
    ws.system_prompt = None
    ws.tenants.acount = AsyncMock(return_value=2)
    ws.tenants.aexists = AsyncMock(return_value=True)
    ws.tenants.all.return_value = _AsyncIter([MagicMock(id="aa"), MagicMock(id="bb")])

    with (
        patch("apps.agents.graph.base.KnowledgeRetriever") as MockKR,
        patch(
            "apps.agents.graph.base._fetch_semantic_model_context",
            new=AsyncMock(
                return_value=(
                    "No data has been loaded yet. Call `run_materialization` to start loading."
                )
            ),
        ),
    ):
        MockKR.return_value.retrieve = AsyncMock(return_value="")

        # _build_system_prompt returns a (stable, volatile) split (arch #254).
        prompt = "\n".join(await _build_system_prompt(ws, MagicMock()))

    assert "## Data Availability" in prompt
    assert "No data has been loaded yet" in prompt
    assert "run_materialization" in prompt
    # The old "just call list_tables to discover" hint must not be the only signal
    # — that was the bug. The agent should know up front there is no data.
    assert "Call `list_tables` to see all available tables." not in prompt
    assert "list_datasets" in prompt


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.parametrize("coverage_kind", ["normal", "malformed", "legacy", "absent"])
async def test_prompt_discloses_and_clears_exclusions_within_cache_ttl(
    workspace, tenant, user, interactive, coverage_kind
):
    graph_base._system_prompt_cache.clear()
    missing = await Tenant.objects.acreate(provider="commcare", external_id="missing-domain")
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=missing)
    view = await WorkspaceViewSchema.objects.acreate(
        workspace=workspace,
        schema_name="ws_prompt_coverage",
        state=SchemaState.ACTIVE,
        tenant_coverage={
            "included_tenants": [{"tenant_id": str(tenant.id), "external_id": tenant.external_id}],
            "excluded_tenants": [
                {
                    "tenant_id": str(missing.id),
                    "provider": "commcare",
                    "external_id": missing.external_id,
                }
            ],
        },
    )
    if coverage_kind == "malformed":
        await WorkspaceViewSchema.objects.filter(pk=view.pk).aupdate(
            tenant_coverage={"excluded_tenants": [None]}
        )
    elif coverage_kind == "legacy":
        await WorkspaceViewSchema.objects.filter(pk=view.pk).aupdate(tenant_coverage={})
    elif coverage_kind == "absent":
        await view.adelete()
    with (
        patch("apps.agents.graph.base.time.monotonic", return_value=100),
        patch("apps.agents.graph.base.KnowledgeRetriever") as retriever,
        patch(
            "apps.agents.graph.base._fetch_semantic_model_context",
            AsyncMock(return_value="Data is loaded and ready."),
        ),
    ):
        retriever.return_value.retrieve = AsyncMock(return_value="Knowledge")
        stable, degraded = await graph_base._build_system_prompt(workspace, user, interactive)
        await WorkspaceViewSchema.objects.filter(pk=view.pk).aupdate(
            tenant_coverage={
                "included_tenants": [{"tenant_id": str(tenant.id)}, {"tenant_id": str(missing.id)}],
                "excluded_tenants": [],
            }
        )
        stable_again, recovered = await graph_base._build_system_prompt(
            workspace, user, interactive
        )
    if coverage_kind == "malformed":
        assert "coverage is unknown" in degraded.lower()
    elif coverage_kind == "normal":
        assert "missing-domain" in degraded
        assert "excluded" in degraded.lower()
        assert "disclose" in degraded.lower()
    else:
        assert "coverage is unknown" not in degraded.lower()
        assert "Sources excluded" not in degraded
    assert "missing-domain" not in recovered
    assert stable == stable_again
    retriever.return_value.retrieve.assert_awaited_once()
    graph_base._system_prompt_cache.clear()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("coverage_kind", ["excluded", "included", "malformed", "legacy"])
async def test_excluded_source_loading_does_not_block_serving_view(
    workspace, tenant, coverage_kind
):
    other = await Tenant.objects.acreate(provider="commcare", external_id="still-loading")
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=other)
    await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="coverage_ready", state=SchemaState.ACTIVE
    )
    loading = await TenantSchema.objects.acreate(
        tenant=other, schema_name="coverage_loading", state=SchemaState.MATERIALIZING
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=loading, pipeline="sync", state="loading"
    )
    coverage = {
        "included_tenants": [{"tenant_id": str(tenant.id)}],
        "excluded_tenants": [{"tenant_id": str(other.id)}],
    }
    if coverage_kind == "included":
        coverage["included_tenants"].append({"tenant_id": str(other.id)})
    elif coverage_kind == "malformed":
        coverage["included_tenants"] = None
    elif coverage_kind == "legacy":
        coverage = {}
    await WorkspaceViewSchema.objects.acreate(
        workspace=workspace,
        schema_name="ws_coverage_safe",
        state=SchemaState.ACTIVE,
        tenant_coverage=coverage,
    )
    await SemanticModel.objects.acreate(
        workspace=workspace, name="Available", status=SemanticModel.Status.ACTIVE
    )
    result = await _fetch_semantic_model_context(workspace)
    if coverage_kind == "excluded":
        assert "previously loaded data" in result
        assert "do not call other data tools" not in result.lower()
    else:
        assert "do not call other data tools" in result.lower()
