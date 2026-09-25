"""One effective load per tenant across sibling workspaces and queued requests.

Real control PostgreSQL throughout: the W and T advisory locks, the generation
ledger and the candidate rows are all real. Only the provider fetch
(``_run_pipeline_with_progress``), credentials, candidate DDL and the Cube build
are stubbed.
"""

import asyncio
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest
from django.utils import timezone

from apps.common.error_codes import ErrorCode
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
from tests.tenant_lock_probe import try_tenant_data_lock

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


@pytest.fixture(autouse=True)
def _no_candidate_ddl(no_candidate_ddl):
    """Shared stub: see tests.pipeline_doubles.no_candidate_ddl."""


@asynccontextmanager
async def _loads(pipeline: _Pipeline):
    with (
        patch("apps.workspaces.tasks._run_pipeline_with_progress", side_effect=pipeline),
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            AsyncMock(return_value={"type": "api_key", "value": "k"}),
        ),
        patch(
            "apps.workspaces.tasks.build_and_promote_cube_schema",
            return_value=MagicMock(id="cube", content_hash="hash"),
        ),
        patch("apps.workspaces.tasks._rebuild_dependent_view_schemas", AsyncMock()),
        patch("apps.workspaces.tasks.teardown_schema.configure") as retire,
        patch("apps.workspaces.tasks.drop_abandoned_candidate.configure") as drop,
    ):
        retire.return_value.defer_async = AsyncMock(return_value=1)
        yield drop.return_value.defer


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
    drop.assert_not_called()


@pytest.mark.parametrize("phase", ["begin", "open"])
async def test_cancellation_during_candidate_setup_clears_committed_state(
    workspace, tenant, user, phase
):
    operation_name = "begin_load_generation" if phase == "begin" else "open_workspace_candidate"
    original = getattr(workspaces_tasks, operation_name)
    entered = threading.Event()
    release = threading.Event()

    def commit_then_wait(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        release.wait(5)
        return result

    async with _loads(_Pipeline()):
        with patch(f"apps.workspaces.tasks.{operation_name}", side_effect=commit_then_wait):
            task = asyncio.create_task(_run(workspace, user))
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

    generation = await TenantLoadGeneration.objects.aget(tenant=tenant)
    assert generation.loading_generation == 0
    candidates = [
        schema
        async for schema in TenantSchema.objects.filter(
            tenant=tenant, load_workspace_id=workspace.id
        )
    ]
    assert [schema.state for schema in candidates] == (
        [SchemaState.FAILED] if phase == "open" else []
    )


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
    drop.assert_called_once_with(schema_id=str(first_candidate))


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
        with (
            patch("apps.workspaces.tasks.tenant_data_lock", add_tenant_after_locking),
            patch("apps.workspaces.tasks.SchemaManager.build_view_schema") as build,
        ):
            build.return_value.tenant_coverage = {}
            result = await _run(workspace, user)

    assert [call[0] for call in pipeline.calls] == [tenant.id]
    late_entry = next(e for e in result["tenants"] if e.get("tenant_id") == str(late.id))
    assert late_entry["success"] is False
    assert "added while the load was starting" in late_entry["error"]
    assert late_entry["error_code"] == ErrorCode.WORKSPACE_SOURCES_CHANGED


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
        # Both intents up front, so the fetch count does not depend on which
        # workspace reaches begin_load_generation first.
        ab_intent, ba_intent = await _intent(ab), await _intent(ba)
        with patch("apps.workspaces.tasks.SchemaManager.build_view_schema") as build:
            build.return_value.tenant_coverage = {}
            results = await asyncio.wait_for(
                asyncio.gather(
                    _run(ab, user, load_intent=ab_intent), _run(ba, user, load_intent=ba_intent)
                ),
                timeout=60,
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


async def test_a_candidate_drop_defers_without_waiting_while_a_writer_holds_t(workspace, tenant):
    candidate = await _failed_workspace_candidate(tenant, workspace)

    with (
        try_tenant_data_lock(tenant.id) as held,
        patch("apps.workspaces.tasks.SchemaManager.teardown") as teardown,
        patch("apps.workspaces.tasks.drop_abandoned_candidate.configure") as retry,
    ):
        assert held
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await workspaces_tasks.drop_abandoned_candidate(schema_id=str(candidate.id), attempt=2)

    teardown.assert_not_called()
    retry.assert_called_once_with(
        schedule_in={"seconds": workspaces_tasks._CANDIDATE_DROP_DELAY_SECONDS}
    )
    retry.return_value.defer_async.assert_awaited_once_with(schema_id=str(candidate.id), attempt=2)


async def test_a_failed_drop_retries_with_backoff_then_gives_up(workspace, tenant):
    candidate = await _failed_workspace_candidate(tenant, workspace)

    with (
        patch(
            "apps.workspaces.tasks.SchemaManager.teardown",
            side_effect=psycopg.OperationalError("server closed the connection"),
        ),
        patch("apps.workspaces.tasks.drop_abandoned_candidate.configure") as retry,
    ):
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await workspaces_tasks.drop_abandoned_candidate(schema_id=str(candidate.id), attempt=2)
        retry.assert_called_once_with(schedule_in={"seconds": 240})
        retry.return_value.defer_async.assert_awaited_once_with(
            schema_id=str(candidate.id), attempt=3
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


async def test_reusing_a_generation_resets_its_inactivity_clock(workspace, tenant, user):
    sibling = await _sibling(user, tenant)
    first_intent = await _intent(workspace)
    second_intent = await _intent(sibling)
    pipeline = _Pipeline()
    async with _loads(pipeline):
        await _run(workspace, user, load_intent=first_intent)
        [served] = await _active_schemas(tenant)
        stale = timezone.now() - timedelta(hours=23)
        await TenantSchema.objects.filter(id=served.id).aupdate(last_accessed_at=stale)
        reused = await _run(sibling, user, load_intent=second_intent)

    assert "reused_generation" in reused["tenants"][0]
    await served.arefresh_from_db()
    assert served.last_accessed_at > stale + timedelta(hours=1)


async def test_an_abandoned_candidate_drop_waits_out_the_writers_lock(workspace, tenant, user):
    pipeline = _Pipeline()
    async with _loads(pipeline):
        await _run(workspace, user)
        old = await TenantSchema.objects.acreate(
            tenant=tenant,
            schema_name="old_generation_candidate",
            state=SchemaState.FAILED,
            load_workspace_id=workspace.id,
            load_generation=1,
            load_config_fingerprint="old",
        )
        with patch("apps.workspaces.tasks.drop_abandoned_candidate.configure") as drop:
            await _run(workspace, user)

    drop.assert_called_once_with(schedule_in={"seconds": 15 * 60})
    drop.return_value.defer.assert_called_once_with(schema_id=str(old.id))


async def test_a_drop_that_hits_a_query_bug_surfaces_instead_of_retrying(workspace, tenant):
    candidate = await _failed_workspace_candidate(tenant, workspace)

    with (
        patch(
            "apps.workspaces.tasks.SchemaManager.teardown",
            side_effect=psycopg.errors.UndefinedColumn("no such column"),
        ),
        patch("apps.workspaces.tasks.drop_abandoned_candidate.configure") as retry,
        pytest.raises(psycopg.errors.UndefinedColumn),
    ):
        await workspaces_tasks.drop_abandoned_candidate(schema_id=str(candidate.id))

    retry.assert_not_called()


@pytest.mark.parametrize("state", [SchemaState.FAILED, SchemaState.EXPIRED])
async def test_the_resume_never_reports_rows_of_an_unpublished_candidate_as_loaded(
    workspace, tenant, user, state
):
    """A failed load's run records its committed sources, but they live in a
    candidate nothing serves; the resume must not present them as loaded."""
    candidate = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name="failed_candidate",
        state=state,
        load_workspace_id=workspace.id,
        load_generation=1,
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=candidate,
        pipeline="commcare_sync",
        procrastinate_job_id=404,
        state=MaterializationRun.RunState.PARTIAL,
        result={
            "sources": {
                "users": {"state": "completed", "rows": 100},
                "visits": {"state": "failed", "rows": 0, "error": "timeout"},
            }
        },
    )

    status, summary = await workspaces_tasks._aggregate_materialization_state(
        404, workspace, str(user.id)
    )

    [entry] = summary
    assert status == "partial"
    assert entry["published"] is False
    assert entry["materialized_row_counts"] == {}
    assert entry["sources"]["users"]["state"] == "not_published"


async def test_the_resume_reports_a_retired_published_candidate_as_published(
    workspace, tenant, user
):
    async with _loads(_Pipeline()):
        await _run(workspace, user, job_id=405)
    [schema] = await _active_schemas(tenant)
    schema.state = SchemaState.EXPIRED
    await schema.asave(update_fields=["state"])
    run = await MaterializationRun.objects.aget(tenant_schema=schema, procrastinate_job_id=405)
    run.result = {"sources": {"users": {"state": "completed", "rows": 100}}}
    await run.asave(update_fields=["result"])

    status, summary = await workspaces_tasks._aggregate_materialization_state(
        405, workspace, str(user.id)
    )

    [entry] = summary
    assert status == "completed"
    assert entry["materialized_row_counts"] == {"users": 100}
    assert entry.get("published") is not False
    assert entry["sources"]["users"]["state"] == "completed"


async def test_an_abort_while_queuing_drops_still_settles_the_candidate(workspace, tenant, user):
    pipeline = _Pipeline()
    async with _loads(pipeline):
        with (
            patch(
                "apps.workspaces.tasks._defer_abandoned_candidate_drops",
                side_effect=asyncio.CancelledError,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await _run(workspace, user)

    [candidate] = [
        s async for s in TenantSchema.objects.filter(tenant=tenant, load_workspace_id=workspace.id)
    ]
    assert candidate.state == SchemaState.FAILED
    ledger = await TenantLoadGeneration.objects.aget(tenant=tenant)
    assert ledger.loading_generation == 0


async def test_abandoning_a_candidate_is_undone_if_its_drop_cannot_be_queued(
    workspace, tenant, user
):
    """Clearing the resume evidence without a queued drop would strand the schema."""
    pipeline = _Pipeline()
    async with _loads(pipeline):
        await _run(workspace, user)
        old = await TenantSchema.objects.acreate(
            tenant=tenant,
            schema_name="old_generation_candidate",
            state=SchemaState.FAILED,
            load_workspace_id=workspace.id,
            load_generation=1,
            load_config_fingerprint="old",
        )
        with patch("apps.workspaces.tasks.drop_abandoned_candidate.configure") as drop:
            drop.return_value.defer.side_effect = RuntimeError("queue unavailable")
            await _run(workspace, user)

    await old.arefresh_from_db()
    assert old.load_config_fingerprint == "old"


@pytest.mark.parametrize(("latest", "intent"), [("failed", "full_refresh"), ("completed", None)])
async def test_a_restore_repair_reloads_when_the_latest_load_did_not_complete(
    workspace, tenant, user, latest, intent
):
    """Restore is offered both for missing data and for data whose latest load
    failed; reconciling would reuse the published generation and change nothing."""
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="served", state=SchemaState.ACTIVE
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=latest,
        result={"sources": {}},
    )

    chosen = await workspaces_tasks._recovery_intent(workspace)

    assert chosen == (intent or "reconcile_missing")


async def test_an_abort_during_failure_cleanup_still_stops_the_run():
    """A cancellation landing while an ordinary failure is being cleaned up must
    propagate once cleanup is done, not be swallowed."""
    started = asyncio.Event()
    finish = asyncio.Event()

    async def cleanup():
        started.set()
        await finish.wait()

    async def fail_and_clean():
        try:
            raise RuntimeError("provider timed out")
        except RuntimeError:
            await workspaces_tasks._drain(cleanup(), "tenant x")
            raise

    task = asyncio.create_task(fail_and_clean())
    await started.wait()
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
