"""One effective load per tenant across sibling workspaces and queued requests.

Real control PostgreSQL throughout: the W and T advisory locks, the generation
ledger and the candidate rows are all real. Only the provider fetch
(``_run_pipeline_with_progress``), credentials, candidate DDL and the Cube build
are stubbed.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.transformations.models import TransformationAsset, TransformationScope
from apps.users.models import Tenant
from apps.workspaces import tasks as workspaces_tasks
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantLoadGeneration,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services.data_operation import LockOrderError
from apps.workspaces.services.load_generations import (
    INTENT_FULL_REFRESH,
    capture_load_intent,
)
from tests.pipeline_doubles import completed_pipeline_run
from tests.tenant_access import agrant_tenant_access

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


class _Pipeline:
    """Records every fetch and the candidate it filled."""

    def __init__(self, fail_times: int = 0):
        self.calls: list[tuple] = []
        self.fail_times = fail_times

    def __call__(self, membership, credential, pipeline, job_id, target_schema=None):
        self.calls.append((membership.tenant_id, target_schema.id if target_schema else None))
        if self.fail_times:
            self.fail_times -= 1
            if target_schema is not None:
                # What a real failed fetch leaves behind: the resume cursors' run.
                MaterializationRun.objects.create(
                    tenant_schema=target_schema,
                    pipeline=pipeline.name,
                    procrastinate_job_id=job_id,
                    state=MaterializationRun.RunState.FAILED,
                    result={"sources": {}},
                )
            raise RuntimeError("provider timed out")
        return completed_pipeline_run(membership, credential, pipeline, job_id, target_schema)


@asynccontextmanager
async def _loads(pipeline: _Pipeline):
    with (
        patch("apps.workspaces.tasks._run_pipeline_with_progress", side_effect=pipeline),
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            AsyncMock(return_value={"type": "api_key", "value": "k"}),
        ),
        patch("apps.workspaces.tasks.SchemaManager.create_physical_schema", return_value=None),
        patch("apps.workspaces.tasks.SchemaManager.teardown", return_value=None),
        patch(
            "apps.workspaces.tasks.build_and_promote_cube_schema",
            return_value=MagicMock(id="cube", content_hash="hash"),
        ),
        patch("apps.workspaces.tasks._rebuild_dependent_view_schemas", AsyncMock()),
        patch("apps.workspaces.tasks.teardown_schema.configure") as retire,
        patch("apps.workspaces.tasks._queue_candidate_drop", new_callable=AsyncMock) as drop,
    ):
        retire.return_value.defer_async = AsyncMock(return_value=1)
        yield drop


async def _sibling(user, tenant, name="Sibling"):
    ws = await Workspace.objects.acreate(name=name, created_by=user)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    return ws


async def _intent(workspace):
    tenant_ids = [
        t
        async for t in WorkspaceTenant.objects.filter(workspace=workspace).values_list(
            "tenant_id", flat=True
        )
    ]
    return await workspaces_tasks._to_thread_fresh_db(
        capture_load_intent, tenant_ids, INTENT_FULL_REFRESH
    )


async def _run(workspace, user, *, job_id=None, load_intent=None):
    return await workspaces_tasks.materialize_workspace_core(
        str(workspace.id), str(user.id), job_id, load_intent=load_intent
    )


async def _active_schemas(tenant):
    return [s async for s in TenantSchema.objects.filter(tenant=tenant, state=SchemaState.ACTIVE)]


async def test_two_workspaces_sharing_a_tenant_perform_one_effective_load(workspace, tenant, user):
    sibling = await _sibling(user, tenant)
    # Both requests are accepted before either load starts, so they join one generation.
    first_intent = await _intent(workspace)
    second_intent = await _intent(sibling)
    assert first_intent == second_intent

    pipeline = _Pipeline()
    async with _loads(pipeline):
        first = await _run(workspace, user, load_intent=first_intent)
        second = await _run(sibling, user, load_intent=second_intent)

    assert len(pipeline.calls) == 1
    assert first["all_succeeded"] is True and second["all_succeeded"] is True
    reused = second["tenants"][0]
    assert reused["reused_generation"] == first_intent[str(tenant.id)]
    assert reused["result"]["reused"] is True
    assert len(await _active_schemas(tenant)) == 1


async def test_duplicate_requests_queued_behind_one_workspace_lock_load_once(
    workspace, tenant, user
):
    first_intent = await _intent(workspace)
    second_intent = await _intent(workspace)

    pipeline = _Pipeline()
    async with _loads(pipeline):
        await _run(workspace, user, load_intent=first_intent)
        second = await _run(workspace, user, load_intent=second_intent)

    assert len(pipeline.calls) == 1
    assert second["tenants"][0]["result"]["reused"] is True


async def test_a_refresh_accepted_after_the_load_completed_loads_again(workspace, tenant, user):
    pipeline = _Pipeline()
    async with _loads(pipeline):
        await _run(workspace, user)
        second = await _run(workspace, user)

    assert len(pipeline.calls) == 2
    assert "reused_generation" not in second["tenants"][0]
    ledger = await TenantLoadGeneration.objects.aget(tenant=tenant)
    assert ledger.published_generation == 2


async def test_a_changed_transform_configuration_is_never_reused(workspace, tenant, user):
    sibling = await _sibling(user, tenant)
    first_intent = await _intent(workspace)
    second_intent = await _intent(sibling)

    pipeline = _Pipeline()
    async with _loads(pipeline):
        await _run(workspace, user, load_intent=first_intent)
        await TransformationAsset.objects.acreate(
            tenant=tenant, name="stg_new", scope=TransformationScope.TENANT, sql_content="select 1"
        )
        second = await _run(sibling, user, load_intent=second_intent)

    assert len(pipeline.calls) == 2
    assert "reused_generation" not in second["tenants"][0]


async def test_a_failed_load_keeps_last_good_serving_and_the_retry_resumes_its_candidate(
    workspace, tenant, user
):
    pipeline = _Pipeline()
    async with _loads(pipeline):
        await _run(workspace, user)
    [last_good] = await _active_schemas(tenant)

    retry_pipeline = _Pipeline(fail_times=1)
    async with _loads(retry_pipeline) as drop:
        failed = await _run(workspace, user)
        assert failed["all_succeeded"] is False
        # The previous data is still what everyone reads.
        assert [s.id for s in await _active_schemas(tenant)] == [last_good.id]

        retried = await _run(workspace, user)

    assert retried["all_succeeded"] is True
    assert retried["tenants"][0]["result"]["resumed"] is True
    first_candidate, second_candidate = (call[1] for call in retry_pipeline.calls)
    assert first_candidate == second_candidate
    [active] = await _active_schemas(tenant)
    assert active.id == first_candidate
    drop.assert_not_awaited()


async def test_a_resumed_generation_is_reused_by_a_request_that_joined_it(workspace, tenant, user):
    """The failed attempt's run is superseded at promotion, so the generation the
    resume published is reusable instead of forcing every joiner to reload."""
    sibling = await _sibling(user, tenant)
    first_intent = await _intent(workspace)
    second_intent = await _intent(sibling)

    pipeline = _Pipeline(fail_times=1)
    async with _loads(pipeline):
        assert (await _run(workspace, user, load_intent=first_intent))["all_succeeded"] is False
        resumed = await _run(workspace, user, load_intent=first_intent)
        joined = await _run(sibling, user, load_intent=second_intent)

    assert resumed["tenants"][0]["result"]["resumed"] is True
    assert len(pipeline.calls) == 2
    assert "reused_generation" in joined["tenants"][0]


async def test_a_promotion_whose_retirement_cannot_be_queued_is_rolled_back(
    workspace, tenant, user
):
    pipeline = _Pipeline()
    async with _loads(pipeline):
        await _run(workspace, user)
        [last_good] = await _active_schemas(tenant)
        with patch("apps.workspaces.tasks.teardown_schema.configure") as retire:
            retire.return_value.defer.side_effect = RuntimeError("queue unavailable")
            result = await _run(workspace, user)

    assert result["all_succeeded"] is False
    assert [s.id for s in await _active_schemas(tenant)] == [last_good.id]
    candidate = await TenantSchema.objects.aget(id=pipeline.calls[-1][1])
    assert candidate.state == SchemaState.FAILED


async def test_a_retry_with_changed_loader_config_starts_fresh_and_drops_the_old_candidate(
    workspace, tenant, user
):
    pipeline = _Pipeline(fail_times=1)
    async with _loads(pipeline) as drop:
        await _run(workspace, user)
        with patch(
            "apps.workspaces.services.load_generations.implementation_revision",
            return_value="next-deploy",
        ):
            retried = await _run(workspace, user)

    assert retried["all_succeeded"] is True
    first_candidate, second_candidate = (call[1] for call in pipeline.calls)
    assert first_candidate != second_candidate
    [(queued,), _] = drop.await_args
    assert queued.id == first_candidate
    drop.assert_awaited_once()


async def test_a_tenant_added_after_the_locks_were_taken_is_reported_not_loaded(
    workspace, tenant, user
):
    late = await Tenant.objects.acreate(
        provider="commcare", external_id="late-domain", canonical_name="Late"
    )
    await agrant_tenant_access(user, late)
    real_lock = workspaces_tasks.tenant_data_lock

    @asynccontextmanager
    async def add_tenant_after_locking(tenant_ids):
        async with real_lock(tenant_ids):
            await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=late)
            yield

    pipeline = _Pipeline()
    async with _loads(pipeline):
        with patch("apps.workspaces.tasks.tenant_data_lock", add_tenant_after_locking):
            result = await _run(workspace, user)

    assert [call[0] for call in pipeline.calls] == [tenant.id]
    late_entry = next(e for e in result["tenants"] if e.get("tenant_id") == str(late.id))
    assert late_entry["success"] is False
    assert "added while the load was starting" in late_entry["error"]


async def test_workspaces_locking_shared_tenants_in_opposite_order_do_not_deadlock(user):
    a = await Tenant.objects.acreate(provider="commcare", external_id="t-a", canonical_name="A")
    b = await Tenant.objects.acreate(provider="commcare", external_id="t-b", canonical_name="B")
    for t in (a, b):
        await agrant_tenant_access(user, t)
    ab = await Workspace.objects.acreate(name="AB", created_by=user)
    ba = await Workspace.objects.acreate(name="BA", created_by=user)
    for ws, order in ((ab, (a, b)), (ba, (b, a))):
        await WorkspaceMembership.objects.acreate(
            workspace=ws, user=user, role=WorkspaceRole.MANAGE
        )
        for t in order:
            await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t)

    pipeline = _Pipeline()
    async with _loads(pipeline):
        with patch("apps.workspaces.tasks.SchemaManager.build_view_schema") as build:
            build.return_value.tenant_coverage = {}
            results = await asyncio.wait_for(
                asyncio.gather(_run(ab, user), _run(ba, user)), timeout=60
            )

    assert all(r["all_succeeded"] for r in results)
    # Each tenant was fetched once; the second workspace reused both loads.
    assert sorted(call[0] for call in pipeline.calls) == sorted([a.id, b.id])


async def _failed_workspace_candidate(tenant, workspace, *, state=SchemaState.FAILED):
    return await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name=f"abandoned_{tenant.id.hex[:8]}",
        state=state,
        load_workspace_id=workspace.id,
        load_generation=1,
        load_config_fingerprint="old",
    )


async def test_an_abandoned_candidate_is_dropped_and_marked_expired(workspace, tenant):
    candidate = await _failed_workspace_candidate(tenant, workspace)

    with patch("apps.workspaces.tasks.SchemaManager.teardown", return_value=None) as teardown:
        await workspaces_tasks.drop_abandoned_candidate(schema_id=str(candidate.id))

    teardown.assert_called_once()
    await candidate.arefresh_from_db()
    assert candidate.state == SchemaState.EXPIRED


async def test_a_candidate_resumed_before_its_drop_ran_is_left_alone(workspace, tenant):
    candidate = await _failed_workspace_candidate(tenant, workspace, state=SchemaState.PROVISIONING)

    with patch("apps.workspaces.tasks.SchemaManager.teardown", return_value=None) as teardown:
        await workspaces_tasks.drop_abandoned_candidate(schema_id=str(candidate.id))

    teardown.assert_not_called()
    await candidate.arefresh_from_db()
    assert candidate.state == SchemaState.PROVISIONING


async def test_a_failed_drop_retries_with_backoff_then_gives_up(workspace, tenant):
    candidate = await _failed_workspace_candidate(tenant, workspace)

    with (
        patch("apps.workspaces.tasks.SchemaManager.teardown", side_effect=RuntimeError("busy")),
        patch("apps.workspaces.tasks.drop_abandoned_candidate.configure") as retry,
    ):
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await workspaces_tasks.drop_abandoned_candidate(schema_id=str(candidate.id), attempt=2)
        retry.assert_called_once_with(
            schedule_in={"seconds": 240},
            queueing_lock=f"drop_abandoned_candidate:{candidate.id}",
        )
        retry.return_value.defer_async.assert_awaited_once_with(
            schema_id=str(candidate.id), attempt=3, last_attempt_at=""
        )

        retry.reset_mock()
        await workspaces_tasks.drop_abandoned_candidate(
            schema_id=str(candidate.id),
            attempt=workspaces_tasks._CANDIDATE_DROP_MAX_ATTEMPTS - 1,
        )
        retry.return_value.defer_async.assert_not_awaited()

    await candidate.arefresh_from_db()
    assert candidate.state == SchemaState.FAILED


async def test_a_reused_tenant_is_reported_as_served_when_the_chat_resumes(workspace, tenant, user):
    """The reusing job has no run of its own; the resume must read the reused run,
    not report the tenant as "the run recorded nothing for it"."""
    sibling = await _sibling(user, tenant)
    first_intent = await _intent(workspace)
    second_intent = await _intent(sibling)
    pipeline = _Pipeline()
    async with _loads(pipeline):
        await _run(workspace, user, job_id=101, load_intent=first_intent)
        reused = await _run(sibling, user, job_id=202, load_intent=second_intent)

    assert "reused_generation" in reused["tenants"][0]
    assert len(pipeline.calls) == 1
    records = workspaces_tasks._resume_records(reused)
    status, summary = await workspaces_tasks._aggregate_materialization_state(
        202, sibling, str(user.id), records
    )

    assert status == "completed"
    assert [entry["state"] for entry in summary] == ["completed"]


async def test_a_reused_run_of_a_tenant_outside_the_workspace_is_ignored(workspace, tenant, user):
    stranger = await Tenant.objects.acreate(
        provider="commcare", external_id="stranger", canonical_name="Stranger"
    )
    schema = await TenantSchema.objects.acreate(
        tenant=stranger, schema_name="stranger_live", state=SchemaState.ACTIVE
    )
    run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        result={"sources": {}},
    )

    _status, summary = await workspaces_tasks._aggregate_materialization_state(
        303,
        workspace,
        str(user.id),
        [{"tenant_id": str(stranger.id), "provider": "commcare", "reused_run_id": str(run.id)}],
    )

    assert "stranger" not in {entry["tenant"] for entry in summary}


async def test_a_failure_opening_the_candidate_clears_the_loading_marker(workspace, tenant, user):
    pipeline = _Pipeline()
    async with _loads(pipeline):
        with patch(
            "apps.workspaces.tasks.open_workspace_candidate",
            side_effect=RuntimeError("schema name collision"),
        ):
            result = await _run(workspace, user)

    assert result["all_succeeded"] is False
    ledger = await TenantLoadGeneration.objects.aget(tenant=tenant)
    assert ledger.loading_generation == 0
    assert ledger.requested_generation > ledger.published_generation


async def test_a_source_added_mid_run_reports_the_view_build_plainly(user):
    a = await Tenant.objects.acreate(provider="commcare", external_id="mid-a", canonical_name="A")
    b = await Tenant.objects.acreate(provider="commcare", external_id="mid-b", canonical_name="B")
    for t in (a, b):
        await agrant_tenant_access(user, t)
    ws = await Workspace.objects.acreate(name="Mid", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    for t in (a, b):
        await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t)

    pipeline = _Pipeline()
    async with _loads(pipeline):
        with patch(
            "apps.workspaces.tasks.SchemaManager.build_view_schema",
            side_effect=LockOrderError("Cannot expand held tenant locks"),
        ):
            result = await _run(ws, user)

    assert result["view_schema"]["ok"] is False
    assert result["view_schema"]["error"] == workspaces_tasks._SOURCE_ADDED_DURING_LOAD


async def test_a_publish_queues_the_demoted_schemas_teardown(workspace, tenant, user):
    pipeline = _Pipeline()
    async with _loads(pipeline):
        await _run(workspace, user)
        [first] = await _active_schemas(tenant)
        with patch("apps.workspaces.tasks.teardown_schema.configure") as retire:
            await _run(workspace, user)

    retire.assert_called_once_with(schedule_in={"seconds": 30 * 60})
    retire.return_value.defer.assert_called_once_with(schema_id=str(first.id))
