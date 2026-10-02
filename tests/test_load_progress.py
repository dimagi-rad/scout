"""A load's progress and freshness reach every workspace member (#369, #411)."""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import AsyncClient, Client
from django.test.utils import CaptureQueriesContext

from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services.load_progress import progress_payload
from tests.tenant_access import ausable_connection, grant_tenant_access

User = get_user_model()
RunState = MaterializationRun.RunState


async def _member(workspace, email, role):
    user = await User.objects.acreate_user(email=email, password="x")
    await WorkspaceMembership.objects.acreate(workspace=workspace, user=user, role=role)
    return user


async def _sources(workspace, members, count, *, load_workspace=True):
    """``count`` sources on the workspace, each member covered, one schema each."""
    schemas = []
    for i in range(count):
        tenant = await Tenant.objects.acreate(
            external_id=f"src-{i}", provider="commcare", canonical_name=f"Source {i}"
        )
        await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=tenant)
        for user in members:
            await TenantMembership.objects.acreate(
                user=user, tenant=tenant, connection=await ausable_connection(user, tenant.provider)
            )
        schemas.append(
            await TenantSchema.objects.acreate(
                tenant=tenant,
                schema_name=f"s_{workspace.id.hex[:8]}_{i}",
                state=SchemaState.PROVISIONING,
                load_workspace_id=workspace.id if load_workspace else None,
            )
        )
    return schemas


async def _active_jobs(email, workspace):
    client = AsyncClient()
    await client.alogin(email=email, password="x")
    resp = await client.get(f"/api/workspaces/{workspace.id}/jobs/active/")
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_member_sees_another_members_load():
    owner = await User.objects.acreate_user(email="a@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=owner)
    await WorkspaceMembership.objects.acreate(
        workspace=ws, user=owner, role=WorkspaceRole.READ_WRITE
    )
    watcher = await _member(ws, "watcher@b.c", WorkspaceRole.READ)
    schemas = await _sources(ws, [owner, watcher], 1)
    thread = await Thread.objects.acreate(workspace=ws, user=owner)
    await ThreadJob.objects.acreate(
        thread=thread, job_type="materialization", procrastinate_job_id=2001, tool_call_id="tc"
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schemas[0],
        pipeline="commcare_sync",
        state=RunState.LOADING,
        procrastinate_job_id=2001,
        progress={"step": 2, "total_steps": 5, "message": "Loading cases"},
    )

    body = await _active_jobs("watcher@b.c", ws)

    assert body["jobs"] == []
    [load] = body["workspace_loads"]
    assert load["tenant_name"] == "Source 0"
    assert load["progress"]["step"] == 2


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_without_a_thread_job_is_visible():
    user = await User.objects.acreate_user(email="a@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE
    )
    [schema] = await _sources(ws, [user], 1, load_workspace=False)
    schema.refresh_workspace_id = ws.id
    await schema.asave(update_fields=["refresh_workspace_id"])
    await MaterializationRun.objects.acreate(
        tenant_schema=schema, pipeline="commcare_sync", state=RunState.LOADING
    )

    body = await _active_jobs("a@b.c", ws)

    [load] = body["workspace_loads"]
    assert (load["source_index"], load["source_total"]) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_four_source_load_reports_source_position():
    user = await User.objects.acreate_user(email="a@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE
    )
    schemas = await _sources(ws, [user], 4)
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    await ThreadJob.objects.acreate(
        thread=thread, job_type="materialization", procrastinate_job_id=3001, tool_call_id="tc"
    )
    for schema, state in zip(
        schemas[:3], [RunState.COMPLETED, RunState.FAILED, RunState.LOADING], strict=True
    ):
        await MaterializationRun.objects.acreate(
            tenant_schema=schema,
            pipeline="commcare_sync",
            state=state,
            procrastinate_job_id=3001,
            progress={"step": 1, "total_steps": 4},
        )

    body = await _active_jobs("a@b.c", ws)

    [load] = body["workspace_loads"]
    assert (load["source_index"], load["source_total"]) == (3, 4)
    [job] = body["jobs"]
    assert (job["source_index"], job["source_total"], job["tenant_name"]) == (3, 4, "Source 2")


def _insert_job(args_json):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, status, args) "
            "VALUES ('default', 'test_task', 'doing'::procrastinate_job_status, %s::jsonb) "
            "RETURNING id",
            [args_json],
        )
        return cursor.fetchone()[0]


def _delete_job(job_id):
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id = %s", [job_id])


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_adding_a_source_loads_only_the_unserved_ones():
    user = await User.objects.acreate_user(email="a@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE
    )
    schemas = await _sources(ws, [user], 4)
    for served in schemas[:3]:
        served.state = SchemaState.ACTIVE
        await served.asave(update_fields=["state"])
    job_id = await sync_to_async(_insert_job)('{"only_unserved": true}')
    try:
        thread = await Thread.objects.acreate(workspace=ws, user=user)
        await ThreadJob.objects.acreate(
            thread=thread,
            job_type="materialization",
            procrastinate_job_id=job_id,
            tool_call_id="tc",
        )
        await MaterializationRun.objects.acreate(
            tenant_schema=schemas[3],
            pipeline="commcare_sync",
            state=RunState.LOADING,
            procrastinate_job_id=job_id,
        )

        body = await _active_jobs("a@b.c", ws)
    finally:
        await sync_to_async(_delete_job)(job_id)

    [load] = body["workspace_loads"]
    assert (load["source_index"], load["source_total"]) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_another_workspaces_load_of_a_shared_source_is_not_ours():
    user = await User.objects.acreate_user(email="a@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE
    )
    other = await Workspace.objects.acreate(name="Other", created_by=user)
    [schema] = await _sources(ws, [user], 1, load_workspace=False)
    schema.load_workspace_id = other.id
    await schema.asave(update_fields=["load_workspace_id"])
    await MaterializationRun.objects.acreate(
        tenant_schema=schema, pipeline="commcare_sync", state=RunState.LOADING
    )

    body = await _active_jobs("a@b.c", ws)

    assert body["workspace_loads"] == []


def _make_loading_workspace(user, index):
    tenant = Tenant.objects.create(
        provider="commcare", external_id=f"prog-{index}", canonical_name=f"Prog {index}"
    )
    ws = Workspace.objects.create(name=tenant.canonical_name, created_by=user)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    grant_tenant_access(user, tenant)
    schema = TenantSchema.objects.create(
        tenant=tenant, schema_name=f"prog_{index}", state=SchemaState.PROVISIONING
    )
    MaterializationRun.objects.create(
        tenant_schema=schema, pipeline="commcare_sync", state=RunState.LOADING
    )
    return ws


@pytest.mark.django_db
def test_list_and_detail_expose_in_progress(user, workspace, tenant):
    client = Client()
    client.force_login(user)
    idle = client.get("/api/workspaces/").json()
    assert next(w for w in idle if w["id"] == str(workspace.id))["in_progress"] is False
    assert client.get(f"/api/workspaces/{workspace.id}/").json()["in_progress"] is False

    schema = TenantSchema.objects.create(
        tenant=tenant, schema_name="prog_x", state=SchemaState.PROVISIONING
    )
    MaterializationRun.objects.create(
        tenant_schema=schema, pipeline="commcare_sync", state=RunState.LOADING
    )

    busy = client.get("/api/workspaces/").json()
    assert next(w for w in busy if w["id"] == str(workspace.id))["in_progress"] is True
    assert client.get(f"/api/workspaces/{workspace.id}/").json()["in_progress"] is True


@pytest.mark.django_db
@pytest.mark.parametrize("large_n", [16, 48])
def test_list_query_count_with_loading_workspaces_does_not_scale(user, workspace, large_n):
    client = Client()
    client.force_login(user)

    small_n = 4
    for i in range(small_n - 1):
        _make_loading_workspace(user, i)
    with CaptureQueriesContext(connection) as small:
        assert len(client.get("/api/workspaces/").json()) == small_n

    for i in range(100, 100 + large_n - small_n):
        _make_loading_workspace(user, i)
    with CaptureQueriesContext(connection) as large:
        body = client.get("/api/workspaces/").json()
    assert len(body) == large_n
    assert sum(w["in_progress"] for w in body) == large_n - 1

    assert len(large) == len(small), (
        f"list endpoint grew from {len(small)} to {len(large)} queries "
        f"between {small_n} and {large_n} workspaces"
    )


@pytest.mark.parametrize(
    ("rows_loaded", "rows_total", "percent"),
    [(500, 1000, 50), (1000, 1000, 100), (1100, 1000, 100), (10, None, None)],
)
def test_progress_percent_is_capped_at_100(rows_loaded, rows_total, percent):
    # Discovery totals can trail the live export when visits arrive mid-load.
    payload = progress_payload({"rows_loaded": rows_loaded, "rows_total": rows_total})
    assert payload["percent"] == percent
