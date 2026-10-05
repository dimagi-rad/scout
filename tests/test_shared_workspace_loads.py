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
from apps.users.models import Tenant, TenantMembership
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
from apps.workspaces.services import load_outcome, retirement
from apps.workspaces.services.data_operation import (
    LockOrderError,
    tenant_data_lock,
    workspace_data_lock,
)
from apps.workspaces.services.load_generations import (
    INTENT_FULL_REFRESH,
    capture_load_intent,
    pipeline_fingerprint,
)
from tests.pipeline_doubles import completed_pipeline_run
from tests.tenant_access import agrant_tenant_access
from tests.tenant_lock_probe import try_tenant_data_lock

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


class _Pipeline:
    """Records every fetch and the candidate it filled."""

    def __init__(self, fail_times: int = 0):
        self.calls: list[tuple] = []
        self.fingerprints: list[str] = []
        self.configs: list = []
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
        result = completed_pipeline_run(membership, credential, pipeline, job_id, target_schema)
        self.fingerprints.append(result["load_fingerprint"])
        self.configs.append(pipeline)
        return result


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
        patch("apps.workspaces.services.publication.rebuild_dependent_view_schemas", AsyncMock()),
        patch("apps.workspaces.services.retirement.configure_teardown_schema") as retire,
        patch("apps.workspaces.services.retirement._queue_candidate_drop_sync") as drop,
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
        with patch("apps.workspaces.services.retirement.configure_teardown_schema") as retire:
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
            "apps.workspaces.services.load_generations.raw_load_revision",
            return_value="next-deploy",
        ):
            retried = await _run(workspace, user)

    assert retried["all_succeeded"] is True
    first_candidate, second_candidate = (call[1] for call in pipeline.calls)
    assert first_candidate != second_candidate
    drop.assert_called_once()
    [(queued,), _] = drop.call_args
    assert queued.id == first_candidate


async def test_a_retry_after_a_transform_only_deploy_resumes_the_failed_candidate(
    workspace, tenant, user
):
    pipeline = _Pipeline(fail_times=1)
    async with _loads(pipeline) as drop:
        await _run(workspace, user)
        with patch(
            "apps.workspaces.services.load_generations.transform_revision",
            return_value="next-deploy",
        ):
            retried = await _run(workspace, user)
        before_deploy = await workspaces_tasks._to_thread_fresh_db(
            pipeline_fingerprint, pipeline.configs[-1], tenant
        )

    assert retried["tenants"][0]["result"]["resumed"] is True
    first_candidate, second_candidate = (call[1] for call in pipeline.calls)
    assert first_candidate == second_candidate
    drop.assert_not_called()
    generation = await TenantLoadGeneration.objects.aget(tenant=tenant)
    assert generation.published_schema_id == second_candidate
    # The resumed run fingerprinted (and so re-ran) the new transform code.
    assert generation.published_fingerprint == pipeline.fingerprints[-1] != before_deploy


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
        patch("apps.workspaces.services.retirement.configure_drop_abandoned_candidate") as retry,
    ):
        assert held
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await workspaces_tasks.drop_abandoned_candidate(schema_id=str(candidate.id), attempt=2)

    teardown.assert_not_called()
    retry.assert_called_once_with(
        schedule_in={"seconds": retirement._CANDIDATE_DROP_DELAY_SECONDS},
        queueing_lock=f"drop_abandoned_candidate:{candidate.id}",
    )
    retry.return_value.defer_async.assert_awaited_once_with(
        schema_id=str(candidate.id), attempt=2, last_attempt_at="", load_job_id=None, busy_count=1
    )


async def test_a_failed_drop_retries_with_backoff_then_gives_up(workspace, tenant):
    candidate = await _failed_workspace_candidate(tenant, workspace)

    with (
        patch(
            "apps.workspaces.tasks.SchemaManager.teardown",
            side_effect=psycopg.OperationalError("server closed the connection"),
        ),
        patch("apps.workspaces.services.retirement.configure_drop_abandoned_candidate") as retry,
    ):
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await workspaces_tasks.drop_abandoned_candidate(schema_id=str(candidate.id), attempt=2)
        retry.assert_called_once_with(
            schedule_in={"seconds": 240},
            queueing_lock=f"drop_abandoned_candidate:{candidate.id}",
        )
        retry.return_value.defer_async.assert_awaited_once_with(
            schema_id=str(candidate.id),
            attempt=3,
            last_attempt_at="",
            load_job_id=None,
            busy_count=0,
        )

        retry.reset_mock()
        await workspaces_tasks.drop_abandoned_candidate(
            schema_id=str(candidate.id),
            attempt=retirement._CANDIDATE_DROP_MAX_ATTEMPTS - 1,
        )
        retry.return_value.defer_async.assert_not_awaited()

    await candidate.arefresh_from_db()
    assert candidate.state == SchemaState.FAILED


async def test_a_new_source_load_only_loads_sources_that_serve_nothing(workspace, tenant, user):
    """Adding a source loads it before publication; sources already serving data
    are published as they are, not reloaded for it."""
    pipeline = _Pipeline()
    async with _loads(pipeline):
        await _run(workspace, user)
        new_source = await Tenant.objects.acreate(
            provider="commcare", external_id="new-source", canonical_name="New"
        )
        await agrant_tenant_access(user, new_source)
        await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=new_source)
        # Serving but near its inactivity TTL: publishing over it must count as use.
        touched_before = timezone.now() - timedelta(hours=23)
        await TenantSchema.objects.filter(tenant=tenant, state=SchemaState.ACTIVE).aupdate(
            last_accessed_at=touched_before
        )
        with (
            patch("apps.workspaces.tasks.SchemaManager.build_view_schema") as build,
            patch(
                "apps.workspaces.services.publication.rebuild_dependent_view_schemas",
                new_callable=AsyncMock,
            ) as dependents,
        ):
            build.return_value.tenant_coverage = {}
            result = await workspaces_tasks.materialize_workspace_core(
                str(workspace.id), str(user.id), None, only_unserved=True
            )

    # Siblings of the untouched source keep valid views; only the loaded one fans out.
    assert list(dependents.await_args.args[0]) == [new_source.id]
    [served] = await _active_schemas(tenant)
    assert served.last_accessed_at > touched_before
    assert [call[0] for call in pipeline.calls] == [tenant.id, new_source.id]
    by_tenant = {e.get("tenant_id") or e["tenant"]: e for e in result["tenants"]}
    assert by_tenant[str(tenant.id)]["result"]["status"] == "already_loaded"
    assert result["all_succeeded"] is True
    build.assert_called_once()
    assert len(await _active_schemas(new_source)) == 1


def _denial(workspace_tenants, code=ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE, per_tenant=None):
    """The shape _materialization_write_denial returns: one not-run entry per tenant."""
    per_tenant = per_tenant or {}
    return {
        "status": "denied",
        "error": "Access could not be verified",
        "error_code": code,
        "tenants": [
            workspaces_tasks._preflight_failure(
                t, "Access could not be verified", per_tenant.get(t.external_id, code)
            )
            for t in workspace_tenants
        ],
    }


async def _deny_after_first_tenant(workspace, **kwargs):
    tenants = [t async for t in workspace.tenants.all()]
    return AsyncMock(return_value=_denial(tenants, **kwargs))


async def _run_new_source_load_denied_after_first_tenant(workspace, user, *, only_unserved=True):
    """Run a new-source load whose authority recheck fails from the second tenant on.

    Calls the core without its locking wrapper (the locks are taken here), so
    every denial check comes from the per-tenant loop and none is spent on the
    wrapper's own checks, however many it makes.
    """
    tenant_ids = [t async for t in workspace.tenants.values_list("id", flat=True)]
    async with workspace_data_lock(workspace.id), tenant_data_lock(tenant_ids):
        return await workspaces_tasks.materialize_workspace_core.__wrapped__(
            str(workspace.id),
            str(user.id),
            None,
            load_intent={},
            locked_tenant_ids=frozenset(str(t) for t in tenant_ids),
            only_unserved=only_unserved,
        )


async def _add_sources(workspace, user, *, serving, unserved):
    added = {}
    for name, is_serving in [(n, True) for n in serving] + [(n, False) for n in unserved]:
        source = await Tenant.objects.acreate(provider="commcare", external_id=name)
        await agrant_tenant_access(user, source)
        await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=source)
        if is_serving:
            await TenantSchema.objects.acreate(
                tenant=source, schema_name=f"serving_{source.id.hex}", state=SchemaState.ACTIVE
            )
        added[name] = source
    return added


async def test_new_source_denial_does_not_rebuild_untouched_siblings(workspace, tenant, user):
    await TenantSchema.objects.acreate(
        tenant=tenant, schema_name=f"serving_{tenant.id.hex}", state=SchemaState.ACTIVE
    )
    await _add_sources(workspace, user, serving=["denied-0", "denied-1"], unserved=[])
    pipeline = _Pipeline()
    async with _loads(pipeline):
        with (
            patch(
                "apps.workspaces.tasks._materialization_write_denial",
                await _deny_after_first_tenant(workspace),
            ),
            patch("apps.workspaces.tasks.SchemaManager.build_view_schema") as build,
            patch(
                "apps.workspaces.services.publication.rebuild_dependent_view_schemas", AsyncMock()
            ) as dependents,
        ):
            build.return_value.tenant_coverage = {}
            result = await _run_new_source_load_denied_after_first_tenant(workspace, user)
    # Every source already serves: the denial stops no load, so nothing failed.
    assert result["all_succeeded"] is True
    assert pipeline.calls == []
    assert list(dependents.await_args.args[0]) == []


async def test_a_transient_denial_never_fails_siblings_the_new_source_load_would_skip(
    workspace, tenant, user
):
    """A denial part-way through a new-source load stops further loads, but a
    sibling that already serves data would have been skipped, not loaded: it
    must not be reported failed, or the Cube gate refuses a model over data the
    run never touched."""
    await TenantSchema.objects.acreate(
        tenant=tenant, schema_name=f"serving_{tenant.id.hex}", state=SchemaState.ACTIVE
    )
    added = await _add_sources(workspace, user, serving=["sibling"], unserved=["new-source"])
    serving_ids = {str(tenant.id), str(added["sibling"].id)}
    new_id = str(added["new-source"].id)
    coverage = {
        "included_tenants": [{"tenant_id": t} for t in sorted(serving_ids)],
        "excluded_tenants": [{"tenant_id": new_id}],
    }
    pipeline = _Pipeline()
    async with _loads(pipeline):
        with (
            patch(
                "apps.workspaces.tasks._materialization_write_denial",
                await _deny_after_first_tenant(workspace),
            ),
            patch("apps.workspaces.tasks.SchemaManager.build_view_schema") as build,
            patch(
                "apps.workspaces.tasks._included_tenant_snapshot_state",
                AsyncMock(return_value="safe"),
            ),
            patch("apps.workspaces.tasks.record_cube_schema_build_failure") as cube_failure,
        ):
            build.return_value.tenant_coverage = coverage
            result = await _run_new_source_load_denied_after_first_tenant(workspace, user)

    # Keyed by external id: every entry carries it, loaded or not.
    by_source = {entry["tenant"]: entry for entry in result["tenants"]}
    for serving in (tenant.external_id, "sibling"):
        assert by_source[serving]["success"] is True
        assert by_source[serving]["result"] == {"status": "already_loaded"}
    new_source_loaded = bool(pipeline.calls)
    assert by_source["new-source"]["success"] is new_source_loaded
    # Only the new source can have failed, and it is excluded from the views,
    # so the Cube build goes ahead over the sources that serve.
    cube_failure.assert_not_called()
    assert result["cube_schema"]["ok"] is True
    assert result["all_succeeded"] is new_source_loaded


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
    status, summary = await load_outcome.aggregate_materialization_state(
        202, sibling, str(user.id), records
    )

    assert status == "completed"
    assert [entry["state"] for entry in summary] == ["completed"]


async def test_an_already_serving_tenant_is_reported_as_served_when_the_chat_resumes(
    workspace, tenant, user
):
    """A new-source load passes over serving sources without a run of its own; a
    chat it resumes must still hear that they are served (#408)."""
    pipeline = _Pipeline()
    async with _loads(pipeline):
        await _run(workspace, user, job_id=101, load_intent=await _intent(workspace))
        passed_over = await workspaces_tasks.materialize_workspace_core(
            str(workspace.id), str(user.id), 202, only_unserved=True
        )

    assert passed_over["tenants"][0]["result"]["status"] == "already_loaded"
    assert len(pipeline.calls) == 1
    records = workspaces_tasks._resume_records(passed_over)
    status, summary = await load_outcome.aggregate_materialization_state(
        202, workspace, str(user.id), records
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

    _status, summary = await load_outcome.aggregate_materialization_state(
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
        with patch("apps.workspaces.services.retirement.configure_teardown_schema") as retire:
            await _run(workspace, user)

    retire.assert_called_once_with(schedule_in={"seconds": 30 * 60})
    retire.return_value.defer.assert_called_once_with(schema_id=str(first.id))


@pytest.mark.parametrize(
    ("outcome", "rebuilds"),
    [
        ({"status": "denied", "error": "role changed", "tenants": []}, True),
        ({"tenants": [], "all_succeeded": True, "view_schema": None}, False),
        ({"view_schema": {"ok": True}}, False),
        ({"view_schema": {"ok": False, "error": "transient publication failure"}}, True),
        (RuntimeError("worker lost the database"), True),
    ],
)
async def test_a_new_source_load_that_stops_before_publishing_still_rebuilds_views(
    outcome, rebuilds
):
    """If the new-source load exits before publishing, nothing else would add the
    new source to the views."""
    core = (
        AsyncMock(side_effect=outcome)
        if isinstance(outcome, Exception)
        else AsyncMock(return_value=outcome)
    )
    with (
        patch("apps.workspaces.tasks.materialize_workspace_core", core),
        patch(
            "apps.workspaces.tasks.rebuild_workspace_view_schema.defer_async",
            new_callable=AsyncMock,
        ) as rebuild,
        patch("apps.workspaces.tasks._defer_resume_for_job", new_callable=AsyncMock) as resume,
        patch(
            "apps.workspaces.tasks.aview_schema_buildable",
            new=AsyncMock(return_value=True),
        ),
    ):
        call = workspaces_tasks.materialize_workspace(
            MagicMock(job=MagicMock(id=7)),
            workspace_id="ws",
            user_id="1",
            only_unserved=True,
            notify_thread=False,
        )
        if isinstance(outcome, Exception):
            with pytest.raises(RuntimeError):
                await call
        else:
            await call

    assert rebuild.await_count == (1 if rebuilds else 0)
    resume.assert_not_awaited()


@pytest.mark.parametrize(
    ("sources", "served", "rebuilds"),
    [(2, 0, False), (1, 1, False), (2, 1, True)],
)
async def test_an_unpublished_new_source_load_rebuilds_views_only_when_they_can_build(
    user, sources, served, rebuilds
):
    """A new workspace whose first load was refused has nothing to build views
    from (#353), and a single source needs no view schema at all."""
    ws = await Workspace.objects.acreate(name="Fresh", created_by=user)
    for index in range(sources):
        tenant = await Tenant.objects.acreate(
            provider="commcare", external_id=f"fresh-{index}", canonical_name=f"Fresh {index}"
        )
        await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
        if index < served:
            await TenantSchema.objects.acreate(
                tenant=tenant, schema_name=f"t_fresh_{index}", state=SchemaState.ACTIVE
            )
    denial = {"status": "denied", "error": "verification unavailable", "tenants": []}
    with (
        patch(
            "apps.workspaces.tasks.materialize_workspace_core",
            new=AsyncMock(return_value=denial),
        ),
        patch(
            "apps.workspaces.tasks.rebuild_workspace_view_schema.defer_async",
            new_callable=AsyncMock,
        ) as rebuild,
    ):
        await workspaces_tasks.materialize_workspace(
            MagicMock(job=MagicMock(id=7)),
            workspace_id=str(ws.id),
            user_id=str(user.id),
            only_unserved=True,
            notify_thread=False,
        )

    assert rebuild.await_count == (1 if rebuilds else 0)


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
    async with _loads(pipeline) as queue:
        await _run(workspace, user)
        old = await TenantSchema.objects.acreate(
            tenant=tenant,
            schema_name="old_generation_candidate",
            state=SchemaState.FAILED,
            load_workspace_id=workspace.id,
            load_generation=1,
            load_config_fingerprint="old",
        )
        queue.reset_mock()
        await _run(workspace, user)

    [(queued,)] = [call.args for call in queue.call_args_list]
    assert queued.id == old.id
    assert queue.call_args.kwargs == {"delay": 15 * 60}


async def test_a_drop_that_hits_a_query_bug_surfaces_instead_of_retrying(workspace, tenant):
    candidate = await _failed_workspace_candidate(tenant, workspace)

    with (
        patch(
            "apps.workspaces.tasks.SchemaManager.teardown",
            side_effect=psycopg.errors.UndefinedColumn("no such column"),
        ),
        patch("apps.workspaces.services.retirement.configure_drop_abandoned_candidate") as retry,
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

    status, summary = await load_outcome.aggregate_materialization_state(
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

    status, summary = await load_outcome.aggregate_materialization_state(
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
                "apps.workspaces.services.retirement.defer_abandoned_candidate_drops",
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
        with patch(
            "apps.workspaces.services.retirement._queue_candidate_drop_sync",
            side_effect=RuntimeError("queue unavailable"),
        ):
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


@pytest.mark.parametrize("cause", ["access_lost", "membership_archived"])
async def test_a_denial_that_concerns_a_serving_sibling_still_reports_it(
    workspace, tenant, user, cause
):
    """Only a transient denial for a source the load would merely skip is passed
    over. A sibling the user lost access to, or whose membership went away, keeps
    its failure: that is exactly what the denial is there to surface."""
    await TenantSchema.objects.acreate(
        tenant=tenant, schema_name=f"serving_{tenant.id.hex}", state=SchemaState.ACTIVE
    )
    await _add_sources(workspace, user, serving=["lost-sibling"], unserved=[])
    tenants = [t async for t in workspace.tenants.all()]
    if cause == "access_lost":
        lost = {t.external_id: ErrorCode.WORKSPACE_TENANT_UNREACHABLE for t in tenants}
        denial = _denial(tenants, ErrorCode.WORKSPACE_TENANT_UNREACHABLE, lost)
    else:
        denial = _denial(tenants)

    async def recheck(*_args):
        if cause == "membership_archived":
            # Revoked upstream during the run: the pending source's membership is gone.
            await TenantMembership.objects.filter(user=user).aupdate(archived_at=timezone.now())
        return denial

    pipeline = _Pipeline()
    async with _loads(pipeline):
        with (
            patch("apps.workspaces.tasks._materialization_write_denial", side_effect=recheck),
            patch("apps.workspaces.tasks.SchemaManager.build_view_schema") as build,
        ):
            build.return_value.tenant_coverage = {}
            result = await _run_new_source_load_denied_after_first_tenant(workspace, user)

    # The first source was handled before the recheck; the other one is the one
    # the denial concerns, and it must stay a failure.
    outcomes = sorted(entry["success"] for entry in result["tenants"])
    assert outcomes == [False, True]
    assert result["all_succeeded"] is False


async def test_losing_an_already_handled_source_mid_run_is_never_reported_as_success(
    workspace, tenant, user
):
    """TENANT_ACCESS_LOST names the lost source UNREACHABLE and the rest SKIPPED.
    When the lost one was handled before the recheck, the SKIPPED serving sibling
    is still passed over (no false Cube failure), but the run must not come back
    as a clean success: the user has to be told about the lost source."""
    await TenantSchema.objects.acreate(
        tenant=tenant, schema_name=f"serving_{tenant.id.hex}", state=SchemaState.ACTIVE
    )
    await _add_sources(workspace, user, serving=["fine-sibling"], unserved=[])
    tenants = [t async for t in workspace.tenants.all()]

    async def recheck(*_args):
        # The source handled before this recheck had its schema touched: that is
        # the one the user just lost; the other can still be used.
        handled = {
            str(t)
            async for t in TenantSchema.objects.filter(last_accessed_at__isnull=False).values_list(
                "tenant_id", flat=True
            )
        }
        per_tenant = {
            t.external_id: (
                ErrorCode.WORKSPACE_TENANT_UNREACHABLE
                if str(t.id) in handled
                else ErrorCode.WORKSPACE_TENANT_SKIPPED
            )
            for t in tenants
        }
        return _denial(tenants, ErrorCode.WORKSPACE_TENANT_UNREACHABLE, per_tenant)

    pipeline = _Pipeline()
    async with _loads(pipeline):
        with (
            patch("apps.workspaces.tasks._materialization_write_denial", side_effect=recheck),
            patch("apps.workspaces.tasks.SchemaManager.build_view_schema") as build,
            patch(
                "apps.workspaces.tasks._included_tenant_snapshot_state",
                AsyncMock(return_value="safe"),
            ),
            patch("apps.workspaces.tasks.record_cube_schema_build_failure") as cube_failure,
        ):
            build.return_value.tenant_coverage = {}
            result = await _run_new_source_load_denied_after_first_tenant(workspace, user)

    # Neither source was touched by a failed load, so the Cube gate still builds.
    assert all(entry["result"] == {"status": "already_loaded"} for entry in result["tenants"])
    cube_failure.assert_not_called()
    # But the run was denied for a real reason, and says so.
    assert result["all_succeeded"] is False
    assert result["denied_mid_run"]["error_code"] == ErrorCode.WORKSPACE_TENANT_UNREACHABLE
    assert result["guidance"]


async def test_mid_run_denial_guidance_names_each_source_once(workspace, tenant, user):
    """The pending sources are reported with the denial's own entries; adding the
    denial's list again for guidance must not name them twice."""
    await _add_sources(workspace, user, serving=[], unserved=["second", "third"])
    tenants = [t async for t in workspace.tenants.all()]
    denial = _denial(tenants, ErrorCode.AUTH_TOKEN_EXPIRED)
    pipeline = _Pipeline()
    async with _loads(pipeline):
        with (
            patch(
                "apps.workspaces.tasks._materialization_write_denial",
                AsyncMock(return_value=denial),
            ),
            patch("apps.workspaces.tasks.SchemaManager.build_view_schema") as build,
        ):
            build.return_value.tenant_coverage = {}
            result = await _run_new_source_load_denied_after_first_tenant(
                workspace, user, only_unserved=False
            )

    [line] = result["guidance"]
    for source in tenants:
        assert line.count(source.external_id) == 1, line
    assert result["denied_mid_run"]["error_code"] == ErrorCode.AUTH_TOKEN_EXPIRED


async def test_an_undenied_run_reports_no_mid_run_denial(workspace, tenant, user):
    pipeline = _Pipeline()
    async with _loads(pipeline):
        result = await _run(workspace, user)
    assert result["denied_mid_run"] is None


async def test_a_load_failure_then_a_denial_names_the_failed_source_once(workspace, tenant, user):
    """A source whose load raised is reported by the loop's own entry; the denial's
    entry for it must not be added to the guidance a second time."""
    await _add_sources(workspace, user, serving=[], unserved=["second"])
    tenants = [t async for t in workspace.tenants.all()]
    denial = _denial(tenants, ErrorCode.AUTH_TOKEN_EXPIRED)
    expired = RuntimeError("sign-in expired mid-load")
    expired.code = ErrorCode.AUTH_TOKEN_EXPIRED
    pipeline = _Pipeline()
    async with _loads(pipeline):
        with (
            patch(
                "apps.workspaces.tasks._materialization_write_denial",
                AsyncMock(return_value=denial),
            ),
            patch(
                "apps.workspaces.tasks._load_workspace_candidate",
                AsyncMock(side_effect=expired),
            ),
            patch("apps.workspaces.tasks.SchemaManager.build_view_schema") as build,
        ):
            build.return_value.tenant_coverage = {}
            result = await _run_new_source_load_denied_after_first_tenant(
                workspace, user, only_unserved=False
            )

    assert all(entry.get("tenant_id") for entry in result["tenants"])
    [line] = result["guidance"]
    for source in tenants:
        assert line.count(source.external_id) == 1, line
