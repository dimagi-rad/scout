"""What POST .../recovery/ admits, dedupes and answers, pinned at the HTTP boundary.

Patches only stable seams (the query surface in data_recovery, the shared manager
and the queue app) so the pins hold wherever admission itself lives.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth.models import update_last_login
from django.contrib.auth.signals import user_logged_in
from django.db import IntegrityError, connection
from django.test import AsyncClient
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.artifacts.models import Artifact, ArtifactType
from apps.users.models import Tenant, TenantMembership, User
from apps.workspaces.models import (
    Workspace,
    WorkspaceDataRecovery,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from config.procrastinate import app
from tests.tenant_access import usable_connection

SURFACE = "apps.workspaces.services.data_recovery.artifact_query_surface"
NEEDS_REBUILD = {
    "status": "needs_semantic_rebuild",
    "recovery_action": "semantic_rebuild",
    "physical_status": "active",
    "semantic_status": "missing",
    "queryable": False,
    "message": "The data model needs a rebuild.",
}
FAILED_TO_START = {"error": "Failed to start data recovery"}


@pytest.fixture
def drop_queued_rows(django_db_blocker):
    # procrastinate_jobs is unmanaged: rows committed by transactional tests outlive them.
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute("SELECT COALESCE(MAX(id), 0) FROM procrastinate_jobs")
        (start,) = cursor.fetchone()
    yield
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id > %s", [start])


@pytest.fixture
def setup(db, drop_queued_rows):
    tenant = Tenant.objects.create(
        provider="commcare", external_id="admission-domain", canonical_name="Admission"
    )
    workspace = Workspace.objects.create(name="Admission")
    WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)
    user = User.objects.create_user(email="admission@example.com", password="pass")
    TenantMembership.objects.create(
        user=user, tenant=tenant, connection=usable_connection(user, tenant.provider)
    )
    WorkspaceMembership.objects.create(workspace=workspace, user=user, role=WorkspaceRole.MANAGE)
    artifact = Artifact.objects.create(
        workspace=workspace,
        created_by=user,
        title="Visits",
        artifact_type=ArtifactType.STORY,
        code="",
        conversation_id="admission-thread",
        data={"story_doc": {"schema_version": 1, "blocks": []}},
        semantic_queries=[{"name": "visits", "measures": ["visits.count"]}],
    )
    client = AsyncClient()
    user_logged_in.disconnect(update_last_login)
    try:
        client.force_login(user)
    finally:
        user_logged_in.connect(update_last_login)
    return SimpleNamespace(
        workspace=workspace,
        user=user,
        artifact=artifact,
        client=client,
        url=f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/recovery/",
    )


async def _recoveries(workspace) -> list[WorkspaceDataRecovery]:
    return [r async for r in WorkspaceDataRecovery.objects.filter(workspace=workspace)]


async def _workspace_jobs(workspace) -> list[ProcrastinateJob]:
    ids = [str(r.id) for r in await _recoveries(workspace)]
    return [job async for job in ProcrastinateJob.objects.filter(args__recovery_id__in=ids)]


async def _jobs_for(recovery) -> list[ProcrastinateJob]:
    return [
        job
        async for job in ProcrastinateJob.objects.filter(
            args__contains={"recovery_id": str(recovery.id)}
        )
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_an_artifact_without_queries_answers_its_state_and_starts_nothing(setup):
    await Artifact.objects.filter(id=setup.artifact.id).aupdate(semantic_queries=[])

    response = await setup.client.post(setup.url, data={})

    assert response.status_code == 200
    assert response.json() == (await setup.client.get(setup.url)).json()
    assert response.json()["status"] == "not_required"
    assert await _recoveries(setup.workspace) == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_ready_surface_answers_its_state_and_starts_nothing(setup):
    ready = {"status": "ready", "queryable": True, "recovery_action": None}
    with patch(SURFACE, new=AsyncMock(return_value=ready)):
        response = await setup.client.post(setup.url, data={})
        current = await setup.client.get(setup.url)

    assert response.status_code == 200
    assert response.json() == current.json() == {**ready, "can_retry": False, "recovery": None}
    assert await _recoveries(setup.workspace) == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_surface_with_no_repair_answers_409_with_its_state(setup):
    unavailable = {
        "status": "unavailable",
        "queryable": False,
        "recovery_action": None,
        "message": "This workspace has no data sources to restore.",
    }
    with patch(SURFACE, new=AsyncMock(return_value=unavailable)):
        response = await setup.client.post(setup.url, data={})

    assert response.status_code == 409
    assert response.json() == {
        "error": unavailable["message"],
        "data_recovery": {**unavailable, "can_retry": False, "recovery": None},
    }
    assert await _recoveries(setup.workspace) == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_needed_repair_is_recorded_queued_and_answered_202(setup):
    with patch(SURFACE, new=AsyncMock(return_value=NEEDS_REBUILD)):
        response = await setup.client.post(setup.url, data={})
        current = await setup.client.get(setup.url)

    assert response.status_code == 202
    assert response.json() == current.json()
    body = response.json()
    assert body["status"] == "recovering"
    assert body["can_retry"] is False
    [recovery] = await _recoveries(setup.workspace)
    assert body["recovery"]["id"] == str(recovery.id)
    assert body["recovery"]["type"] == "semantic_rebuild"
    assert recovery.requested_by_id == setup.user.id
    assert recovery.recovery_type == "semantic_rebuild"
    assert recovery.source_type == "artifact"
    assert recovery.source_id == setup.artifact.id
    assert recovery.state == WorkspaceDataRecovery.State.PENDING
    [job] = await _jobs_for(recovery)
    assert recovery.procrastinate_job_id == job.id


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_second_post_joins_the_active_repair(setup):
    with patch(SURFACE, new=AsyncMock(return_value=NEEDS_REBUILD)):
        first = await setup.client.post(setup.url, data={})
        second = await setup.client.post(setup.url, data={})

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["status"] == "recovering"
    assert second.json()["recovery"]["id"] == first.json()["recovery"]["id"]
    assert len(await _recoveries(setup.workspace)) == 1
    assert len(await _workspace_jobs(setup.workspace)) == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_losing_the_create_race_binds_to_the_winner_without_queueing(setup):
    manager = WorkspaceDataRecovery.objects
    create = manager.acreate

    async def winner_commits_first(**fields):
        winner = await create(**{**fields, "source_id": None})
        setup.winner = winner
        raise IntegrityError("one active recovery per workspace")

    with (
        patch(SURFACE, new=AsyncMock(return_value=NEEDS_REBUILD)),
        patch.object(manager, "acreate", side_effect=winner_commits_first),
    ):
        response = await setup.client.post(setup.url, data={})
        current = await setup.client.get(setup.url)

    assert response.status_code == 200
    assert response.json() == current.json()
    assert response.json()["status"] == "recovering"
    assert response.json()["recovery"]["id"] == str(setup.winner.id)
    assert [r.id for r in await _recoveries(setup.workspace)] == [setup.winner.id]
    assert await _workspace_jobs(setup.workspace) == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_create_conflict_with_no_active_row_answers_500(setup):
    with (
        patch(SURFACE, new=AsyncMock(return_value=NEEDS_REBUILD)),
        patch.object(
            WorkspaceDataRecovery.objects, "acreate", side_effect=IntegrityError("conflict")
        ),
    ):
        response = await setup.client.post(setup.url, data={})

    assert response.status_code == 500
    assert response.json() == FAILED_TO_START
    assert await _recoveries(setup.workspace) == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "recorded"),
    [
        (RuntimeError("queue down"), "queue down"),
        (RuntimeError(), "Failed to start data recovery"),
        (RuntimeError("x" * 1500), "x" * 1000),
    ],
)
async def test_a_dispatch_failure_fails_the_recorded_repair_and_answers_500(
    setup, raised, recorded
):
    with (
        patch(SURFACE, new=AsyncMock(return_value=NEEDS_REBUILD)),
        patch.object(app, "configure_task", side_effect=raised),
    ):
        response = await setup.client.post(setup.url, data={})

    assert response.status_code == 500
    assert response.json() == FAILED_TO_START
    [recovery] = await _recoveries(setup.workspace)
    assert recovery.state == WorkspaceDataRecovery.State.FAILED
    assert recovery.error == recorded
    assert recovery.procrastinate_job_id is None
    assert await _workspace_jobs(setup.workspace) == []
