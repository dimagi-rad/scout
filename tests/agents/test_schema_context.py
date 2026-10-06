"""Tests for schema context injection into the agent system prompt."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.utils import timezone

from apps.agents.graph import prompt_context
from apps.agents.graph.prompt_context import _build_system_prompt, _fetch_semantic_model_context
from apps.chat.models import Thread, ThreadJob
from apps.semantic.models import SemanticModel
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceTenant,
    WorkspaceViewSchema,
)


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

    result = await _fetch_semantic_model_context(
        workspace, interactive=interactive, write_capable=True
    )

    assert "in progress" in result.lower()
    if interactive:
        assert "trigger another" in result.lower()
        assert "will resume automatically" not in result.lower()
        assert "if this conversation" not in result.lower()
        assert "nothing will resume this conversation" in result.lower()
        assert "ask their question again" in result.lower()
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

    result = await _fetch_semantic_model_context(
        workspace, interactive=interactive, write_capable=True
    )

    assert "in progress" in result.lower()
    if has_serving_schema and has_catalog:
        assert "previously loaded data" in result.lower()
        assert "do not trigger another" in result.lower()
        assert "do not call other data tools" not in result.lower()
        assert "semantic_query" in result
        assert "will resume automatically" not in result.lower()
        if interactive:
            assert "nothing will resume this conversation" in result.lower()
        else:
            assert "ask again" not in result.lower()
    else:
        assert "previously loaded data" not in result.lower()
        assert "Data is loaded and ready" not in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_refresh_this_chat_started_promises_its_resume(workspace, tenant, user):
    await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="previous_data", state=SchemaState.ACTIVE
    )
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
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    await ThreadJob.objects.acreate(
        thread=thread,
        job_type=ThreadJob.JobType.MATERIALIZATION,
        procrastinate_job_id=881_000,
        tool_call_id="",
        state=ThreadJob.State.PENDING,
    )

    result = await _fetch_semantic_model_context(
        workspace, interactive=True, write_capable=True, conversation_id=str(thread.id)
    )

    assert "previously loaded data" in result.lower()
    assert "this conversation will resume automatically" in result.lower()
    assert "nothing will resume" not in result.lower()


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

    result = await _fetch_semantic_model_context(workspace, write_capable=True)

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

    result = await _fetch_semantic_model_context(workspace, write_capable=True)

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

    result = await _fetch_semantic_model_context(workspace, write_capable=True)

    assert "Data is loaded and ready" in result
    assert "in progress" not in result.lower()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("interactive", [True, False])
async def test_prompt_availability_changes_within_cache_ttl(workspace, tenant, user, interactive):
    prompt_context._system_prompt_cache.clear()
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="prompt_transition", state=SchemaState.ACTIVE
    )
    await SemanticModel.objects.acreate(
        workspace=workspace, name="Existing model", status=SemanticModel.Status.ACTIVE
    )
    with (
        patch.object(prompt_context, "time", MagicMock(monotonic=MagicMock(return_value=100))),
        patch("apps.agents.graph.prompt_context.KnowledgeRetriever") as retriever,
    ):
        retriever.return_value.retrieve = AsyncMock(return_value="Knowledge")
        stable, ready = await prompt_context._build_system_prompt(
            workspace, user, interactive, write_capable=True
        )
        run = await MaterializationRun.objects.acreate(
            tenant_schema=schema,
            pipeline="commcare_sync",
            state=MaterializationRun.RunState.LOADING,
        )
        stable_loading, loading = await prompt_context._build_system_prompt(
            workspace, user, interactive, write_capable=True
        )
        await MaterializationRun.objects.filter(pk=run.pk).aupdate(
            state=MaterializationRun.RunState.COMPLETED
        )
        stable_done, done = await prompt_context._build_system_prompt(
            workspace, user, interactive, write_capable=True
        )

    assert "Data is loaded and ready" in ready
    assert "in progress" in loading.lower()
    assert "Data is loaded and ready" not in loading
    assert "Data is loaded and ready" in done
    assert "in progress" not in done.lower()
    assert stable == stable_loading == stable_done
    retriever.return_value.retrieve.assert_awaited_once()
    prompt_context._system_prompt_cache.clear()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_build_system_prompt_no_schema_status_call():
    """The assembled system prompt must not instruct the agent to call get_schema_status."""

    mock_workspace = MagicMock()
    mock_workspace.system_prompt = None
    mock_workspace.tenants.acount = AsyncMock(return_value=1)
    mock_workspace.tenants.aexists = AsyncMock(return_value=True)

    with (
        patch("apps.agents.graph.prompt_context.KnowledgeRetriever") as MockKR,
        patch(
            "apps.agents.graph.prompt_context._fetch_semantic_model_context",
            new=AsyncMock(return_value="Data is loaded. Use `list_datasets`."),
        ),
        patch(
            "apps.agents.graph.prompt_context.aworkspace_source_freshness",
            AsyncMock(return_value=[]),
        ),
        patch(
            "apps.agents.graph.prompt_context.aworkspace_memory_fingerprint",
            AsyncMock(return_value=""),
        ),
    ):
        MockKR.return_value.retrieve = AsyncMock(return_value="")

        # _build_system_prompt returns a (stable, volatile) split (arch #254).
        prompt = "\n".join(await _build_system_prompt(mock_workspace, MagicMock()))

    assert "call `get_schema_status`" not in prompt
    assert "start of every conversation" not in prompt
    assert "## Data Availability" in prompt
    assert "list_datasets" in prompt


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


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_build_system_prompt_multi_tenant_no_data_pre_fetched():
    """Multi-tenant workspace with no data: system prompt tells the agent upfront,
    NOT 'call list_tables to discover'."""

    ws = MagicMock()
    ws.id = "22222222-2222-2222-2222-222222222222"
    ws.system_prompt = None
    ws.tenants.acount = AsyncMock(return_value=2)
    ws.tenants.aexists = AsyncMock(return_value=True)
    ws.tenants.all.return_value = _AsyncIter([MagicMock(id="aa"), MagicMock(id="bb")])

    with (
        patch("apps.agents.graph.prompt_context.KnowledgeRetriever") as MockKR,
        patch(
            "apps.agents.graph.prompt_context._fetch_semantic_model_context",
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
    prompt_context._system_prompt_cache.clear()
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
        patch("apps.agents.graph.prompt_context.time.monotonic", return_value=100),
        patch("apps.agents.graph.prompt_context.KnowledgeRetriever") as retriever,
        patch(
            "apps.agents.graph.prompt_context._fetch_semantic_model_context",
            AsyncMock(return_value="Data is loaded and ready."),
        ),
    ):
        retriever.return_value.retrieve = AsyncMock(return_value="Knowledge")
        stable, degraded = await prompt_context._build_system_prompt(
            workspace, user, interactive, write_capable=True
        )
        await WorkspaceViewSchema.objects.filter(pk=view.pk).aupdate(
            tenant_coverage={
                "included_tenants": [{"tenant_id": str(tenant.id)}, {"tenant_id": str(missing.id)}],
                "excluded_tenants": [],
            }
        )
        stable_again, recovered = await prompt_context._build_system_prompt(
            workspace, user, interactive, write_capable=True
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
    prompt_context._system_prompt_cache.clear()


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
        tenant=other, schema_name="coverage_loading", state=SchemaState.PROVISIONING
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
    result = await _fetch_semantic_model_context(workspace, write_capable=True)
    if coverage_kind == "excluded":
        assert "previously loaded data" in result
        assert "do not call other data tools" not in result.lower()
    else:
        assert "do not call other data tools" in result.lower()


async def _unresolvable_workspace(workspace, tenant, *, multi, state, partial=False):
    """Make ``workspace``'s providers unresolvable: all of them, or one of two if ``partial``.

    ``state`` is the serving schema's state, or None for nothing loaded.
    """
    if not partial:
        await Tenant.objects.filter(id=tenant.id).aupdate(provider="retired_provider")
    if multi:
        # Same provider twice when not partial, so each provider is named once.
        other = await Tenant.objects.acreate(
            provider="retired_provider", external_id="retired", canonical_name="Retired"
        )
        await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=other)
        if state is not None:
            await WorkspaceViewSchema.objects.acreate(
                workspace=workspace, schema_name="ws_view", state=state
            )
    elif state is not None:
        await TenantSchema.objects.acreate(tenant=tenant, schema_name="loaded", state=state)


_SERVING_STATES = [None, SchemaState.ACTIVE]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.parametrize("write_capable", [True, False])
@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("state", _SERVING_STATES)
async def test_unresolvable_pipeline_asks_for_an_admin_instead_of_a_rerun(
    workspace, tenant, interactive, write_capable, multi, state
):
    """F2: a re-run fails PIPELINE_UNRESOLVED for a provider with no pipeline, so
    telling the agent to (re-)run materialization only wastes a job every turn."""
    await _unresolvable_workspace(workspace, tenant, multi=multi, state=state)

    context = await _fetch_semantic_model_context(
        workspace, interactive=interactive, write_capable=write_capable
    )

    assert "administrator" in context
    assert context.count("retired_provider") == 1
    assert "Run materialization to rebuild" not in context
    assert "Call `run_materialization`" not in context
    assert "No data has been loaded yet" not in context
    assert "in progress" not in context
    assert "A load skips" not in context
    if not write_capable:
        assert "run_materialization" not in context
    # Loaded data stays queryable: SQL never resolves a pipeline.
    loaded = state == SchemaState.ACTIVE
    assert ("`query` SQL still works" in context) is loaded
    assert ("materialization cannot load it" in context) is not loaded


def _expected_load_guidance(state, *, interactive, write_capable):
    if state == SchemaState.ACTIVE:
        if write_capable and interactive:
            return prompt_context._SEMANTIC_REBUILD_NOT_RUNNING_GUIDANCE
        if write_capable:
            return prompt_context._HEADLESS_LOADED_REBUILD_GUIDANCE
        return prompt_context._READ_ONLY_LOADED_SQL_GUIDANCE
    if not write_capable:
        return prompt_context._READ_ONLY_MATERIALIZE_GUIDANCE
    if interactive:
        return prompt_context._INTERACTIVE_MATERIALIZE_GUIDANCE
    return prompt_context._HEADLESS_MATERIALIZE_GUIDANCE


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.parametrize("write_capable", [True, False])
@pytest.mark.parametrize("state", _SERVING_STATES)
async def test_partly_unresolvable_pipeline_keeps_the_load_guidance(
    workspace, tenant, interactive, write_capable, state
):
    """A load still loads the resolvable sources, so their guidance stands; the
    unresolvable provider only adds a note. Asserting the exact guidance catches a
    mixed workspace regressing to the fully-unresolved text (G13)."""
    await _unresolvable_workspace(workspace, tenant, multi=True, state=state, partial=True)

    context = await _fetch_semantic_model_context(
        workspace, interactive=interactive, write_capable=write_capable
    )

    expected = _expected_load_guidance(state, interactive=interactive, write_capable=write_capable)
    assert expected in context
    assert "A load skips this workspace's provider retired_provider sources" in context
    assert "materialization cannot load it" not in context
    assert "materialization cannot rebuild them" not in context
