"""The workspace API and the MCP status agree on which runs are a workspace's active load.

Both projections select active runs and decide which of them the workspace owns; these
cases pin that they answer alike, and the query counts of the bulk paths, so the
selection can move to one shared helper without changing either.
"""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
)
from apps.workspaces.services.load_activity import (
    aworkspace_schema_status,
    workspace_schema_statuses,
)
from apps.workspaces.services.load_progress import (
    aworkspace_load_progress,
    workspace_ids_in_progress,
)
from mcp_server.server import _load_in_progress

RunState = MaterializationRun.RunState
PROVISIONING = SchemaState.PROVISIONING
User = get_user_model()

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


async def _world(user):
    """``mine`` and ``sibling`` share tenant ``shared``; ``mine`` also has ``own``."""
    mine = await Workspace.objects.acreate(name="mine", created_by=user)
    sibling = await Workspace.objects.acreate(name="sibling", created_by=user)
    shared = await Tenant.objects.acreate(
        provider="commcare", external_id="shared", canonical_name="shared"
    )
    own = await Tenant.objects.acreate(provider="commcare", external_id="own", canonical_name="own")
    for workspace, tenant in ((mine, shared), (mine, own), (sibling, shared)):
        await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=tenant)
    return mine, sibling, shared, own


async def _run(tenant, state, *, schema_state=SchemaState.ACTIVE, job_id=None, **markers):
    schema = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name=f"s_{tenant.external_id}_{state}_{job_id}_{'_'.join(markers)}",
        state=schema_state,
        **markers,
    )
    return await MaterializationRun.objects.acreate(
        tenant_schema=schema, pipeline="commcare_sync", state=state, procrastinate_job_id=job_id
    )


async def _http_owned(workspace) -> set[str]:
    return {load["tenant_name"] for load in await aworkspace_load_progress(workspace)}


async def _mcp_owned(workspace) -> set[str]:
    summary = await _load_in_progress(workspace)
    if summary is None:
        return set()
    return {
        source["name"]
        for source in summary["sources"]
        if source["state"] in MaterializationRun.ACTIVE_STATES
    }


async def _agree(workspace, expected: set[str]):
    assert await _http_owned(workspace) == expected
    assert await _mcp_owned(workspace) == expected


async def test_queued_load_has_no_run_yet_so_neither_side_reports_one(user):
    mine, _, _, _ = await _world(user)

    await _agree(mine, set())
    assert not await sync_to_async(workspace_ids_in_progress)([mine.id])


@pytest.mark.parametrize("state", sorted(MaterializationRun.ACTIVE_STATES))
async def test_running_load_candidate_of_the_workspace_is_its_own(user, state):
    mine, _, _, own = await _world(user)
    await _run(own, state, schema_state=PROVISIONING, load_workspace_id=mine.id)

    await _agree(mine, {"own"})
    assert await sync_to_async(workspace_ids_in_progress)([mine.id]) == {mine.id}


async def test_run_found_through_the_workspaces_thread_job_is_its_own(user):
    mine, _, _, own = await _world(user)
    thread = await Thread.objects.acreate(workspace=mine, user=user)
    await ThreadJob.objects.acreate(
        thread=thread, job_type="materialization", procrastinate_job_id=901, tool_call_id="tc"
    )
    await _run(own, RunState.LOADING, job_id=901)

    await _agree(mine, {"own"})


async def test_provisioning_refresh_of_the_workspace_is_its_own(user):
    mine, _, _, own = await _world(user)
    await _run(own, RunState.LOADING, schema_state=PROVISIONING, refresh_workspace_id=mine.id)

    await _agree(mine, {"own"})


async def test_refresh_marker_on_a_serving_schema_is_not_owned(user):
    mine, _, _, own = await _world(user)
    await _run(own, RunState.LOADING, refresh_workspace_id=mine.id)

    await _agree(mine, set())


async def test_run_owned_by_another_workspace_is_not_this_workspaces_load(user):
    mine, sibling, shared, _ = await _world(user)
    await _run(shared, RunState.LOADING, schema_state=PROVISIONING, load_workspace_id=sibling.id)

    await _agree(mine, set())
    await _agree(sibling, {"shared"})
    # "Loading" for the list badge is any active run on a tenant, owned or not.
    in_progress = await sync_to_async(workspace_ids_in_progress)([mine.id, sibling.id])
    assert in_progress == {mine.id, sibling.id}


async def test_sibling_load_shows_only_where_it_is_waited_on(user):
    mine, sibling, shared, own = await _world(user)
    await _run(shared, RunState.LOADING, schema_state=PROVISIONING, load_workspace_id=sibling.id)
    await _run(own, RunState.LOADING, schema_state=PROVISIONING, load_workspace_id=mine.id)

    summary = await _load_in_progress(mine)

    assert {s["name"]: s["state"] for s in summary["sources"]} == {
        "own": "loading",
        "shared": "loading_in_other_workspace",
    }
    assert await _http_owned(mine) == {"own"}


@pytest.mark.parametrize("state", [RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED])
async def test_finished_run_is_no_load_on_either_side(user, state):
    mine, _, _, own = await _world(user)
    await _run(own, state, schema_state=PROVISIONING, load_workspace_id=mine.id)

    await _agree(mine, set())
    assert not await sync_to_async(workspace_ids_in_progress)([mine.id])
    assert await aworkspace_schema_status(mine.id) != "provisioning"


async def test_list_badge_and_schema_status_agree_on_a_running_load(user):
    mine, sibling, _, own = await _world(user)
    await _run(own, RunState.LOADING, schema_state=PROVISIONING, load_workspace_id=mine.id)

    statuses = await sync_to_async(workspace_schema_statuses)([mine.id, sibling.id])

    assert statuses[mine.id] == "provisioning"
    assert statuses[mine.id] == await aworkspace_schema_status(mine.id)
    assert statuses[sibling.id] == await aworkspace_schema_status(sibling.id)


async def test_bulk_paths_use_a_fixed_number_of_queries(user, django_assert_num_queries):
    mine, sibling, _, own = await _world(user)
    await _run(own, RunState.LOADING, schema_state=PROVISIONING, load_workspace_id=mine.id)
    ids = [mine.id, sibling.id]

    def in_progress_query_count():
        with django_assert_num_queries(1):
            return workspace_ids_in_progress(ids)

    assert await sync_to_async(in_progress_query_count)() == {mine.id}

    def statuses_query_count():
        with django_assert_num_queries(6):
            return workspace_schema_statuses(ids)

    assert (await sync_to_async(statuses_query_count)())[mine.id] == "provisioning"
