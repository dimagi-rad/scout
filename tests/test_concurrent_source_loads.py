"""A workspace's sources load concurrently: a lane per job plus shared slots (#927)."""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.test import override_settings

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import WorkspaceTenant
from apps.workspaces.services import data_operation, materialize
from apps.workspaces.services.materialize import materialize_workspace_core
from mcp_server.services.materializer import MaterializationCancelled
from tests.pipeline_doubles import completed_pipeline_run
from tests.tenant_access import grant_tenant_access

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _no_candidate_ddl(no_candidate_ddl):
    """Shared stub: see tests.pipeline_doubles.no_candidate_ddl."""


@pytest.fixture
def three_source_workspace(db, workspace, user):
    for index in (2, 3):
        tenant = Tenant.objects.create(
            provider="commcare", external_id=f"test-domain-{index}", canonical_name=f"D{index}"
        )
        WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)
        grant_tenant_access(user, tenant)
    return workspace


@pytest.fixture
def membership_order(three_source_workspace, user):
    """External ids in the order the load walks the memberships."""
    return [
        tm.tenant.external_id
        for tm in TenantMembership.objects.select_related("tenant").filter(
            user=user, tenant__workspace_tenants__workspace=three_source_workspace
        )
    ]


def _registry():
    pipeline = MagicMock()
    pipeline.name = "commcare_sync"
    pipeline.provider = "commcare"
    registry = MagicMock()
    registry.list.return_value = [pipeline]
    registry.get.return_value = pipeline
    return registry


class _Overlap:
    """Counts how many pipeline calls run at once."""

    def __init__(self):
        self._lock = threading.Lock()
        self.running = 0
        self.peak = 0

    def __enter__(self):
        with self._lock:
            self.running += 1
            self.peak = max(self.peak, self.running)

    def __exit__(self, *exc):
        with self._lock:
            self.running -= 1


async def _load(workspace, user, pipeline, credential=None):
    resolve = AsyncMock(
        side_effect=credential or (lambda tm: {"type": "api_key", "value": "k"}),
    )
    with (
        patch("apps.workspaces.services.materialize.aresolve_credential", resolve),
        patch("apps.workspaces.services.materialize.get_registry", return_value=_registry()),
        patch(
            "apps.workspaces.services.materialize._run_pipeline_with_progress",
            side_effect=pipeline,
        ),
        patch("apps.workspaces.services.materialize.SchemaManager", return_value=MagicMock()),
        patch("apps.workspaces.services.materialize.build_and_promote_cube_schema"),
        patch(
            "apps.workspaces.services.publication.rebuild_dependent_view_schemas",
            new_callable=AsyncMock,
        ),
    ):
        return await materialize_workspace_core(str(workspace.id), str(user.id))


def _all_slots_free(shared):
    slots = materialize._shared_slots_by_loop[asyncio.get_running_loop()]
    return slots.free == shared and not slots.waiters


async def test_sources_load_at_once_and_report_in_membership_order(
    three_source_workspace, user, membership_order
):
    together = threading.Barrier(3, timeout=10)

    def pipeline(membership, *args, **kwargs):
        # Each load co-owns the run's tenant locks from its own thread.
        data_operation.assert_tenant_lock_held(membership.tenant_id)
        together.wait()
        return completed_pipeline_run(membership, *args, **kwargs)

    with override_settings(MATERIALIZATION_SHARED_LOAD_SLOTS=2):
        result = await _load(three_source_workspace, user, pipeline)
        assert _all_slots_free(2)

    assert result["all_succeeded"] is True
    assert [entry["tenant"] for entry in result["tenants"]] == membership_order


async def test_load_slots_bound_how_many_sources_load_at_once(three_source_workspace, user):
    overlap = _Overlap()

    def pipeline(*args, **kwargs):
        with overlap:
            time.sleep(0.2)
        return completed_pipeline_run(*args, **kwargs)

    with override_settings(MATERIALIZATION_SHARED_LOAD_SLOTS=1):
        result = await _load(three_source_workspace, user, pipeline)
        assert _all_slots_free(1)

    assert result["all_succeeded"] is True
    assert overlap.peak == 2


async def test_one_slot_loads_sources_one_after_another(three_source_workspace, user):
    overlap = _Overlap()

    def pipeline(*args, **kwargs):
        with overlap:
            time.sleep(0.05)
        return completed_pipeline_run(*args, **kwargs)

    with override_settings(MATERIALIZATION_SHARED_LOAD_SLOTS=0):
        result = await _load(three_source_workspace, user, pipeline)

    assert result["all_succeeded"] is True
    assert overlap.peak == 1


async def test_a_cancelled_load_stops_sources_not_yet_started(
    three_source_workspace, user, membership_order
):
    first, second, third = membership_order
    first_cancelled = threading.Event()
    calls = []

    def pipeline(membership, *args, **kwargs):
        calls.append(membership.tenant.external_id)
        if membership.tenant.external_id == first:
            first_cancelled.set()
            raise MaterializationCancelled()
        # Still running when the first load's slot frees for the third source.
        first_cancelled.wait(5)
        time.sleep(0.2)
        return completed_pipeline_run(membership, *args, **kwargs)

    with override_settings(MATERIALIZATION_SHARED_LOAD_SLOTS=1):
        result = await _load(three_source_workspace, user, pipeline)
        assert _all_slots_free(1)

    assert sorted(calls) == sorted([first, second])
    by_tenant = {entry["tenant"]: entry for entry in result["tenants"]}
    assert by_tenant[first]["cancelled"] is True
    assert by_tenant[second]["success"] is True
    assert third not in by_tenant
    assert result["all_succeeded"] is False


async def test_a_launch_error_is_raised_as_itself_after_running_loads_finish(
    three_source_workspace, user, membership_order
):
    first, second, _third = membership_order
    finished = []

    def pipeline(membership, *args, **kwargs):
        time.sleep(0.2)
        result = completed_pipeline_run(membership, *args, **kwargs)
        finished.append(membership.tenant.external_id)
        return result

    def credential(tm):
        if tm.tenant.external_id == second:
            raise RuntimeError("credential store unavailable")
        return {"type": "api_key", "value": "k"}

    with override_settings(MATERIALIZATION_SHARED_LOAD_SLOTS=1):
        with pytest.raises(RuntimeError, match="credential store unavailable"):
            await _load(three_source_workspace, user, pipeline, credential=credential)
        assert _all_slots_free(1)

    # The first source was loading when the second failed to start; it is not cancelled.
    assert finished == [first]


async def test_a_job_still_loads_while_other_jobs_hold_every_shared_slot(
    three_source_workspace, user
):
    overlap = _Overlap()

    def pipeline(*args, **kwargs):
        with overlap:
            time.sleep(0.05)
        return completed_pipeline_run(*args, **kwargs)

    with override_settings(MATERIALIZATION_SHARED_LOAD_SLOTS=1):
        other_job = materialize._LoadLanes()
        other_job.take_now()
        release_shared = other_job.take_now()
        assert release_shared is not None
        try:
            result = await _load(three_source_workspace, user, pipeline)
        finally:
            release_shared()

    assert result["all_succeeded"] is True
    assert overlap.peak == 1
