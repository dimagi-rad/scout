from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.chat.models import Thread, ThreadJob
from apps.workspaces.models import Workspace
from apps.workspaces.tasks import expire_stale_thread_jobs

User = get_user_model()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_janitor_defers_resume_for_stale_threadjobs():
    """Janitor defers the resume task first and leaves state unchanged.
    The resume task is responsible for flipping the ThreadJob state so the
    user gets an agent follow-up message instead of a silent FAILED."""
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=9999, tool_call_id="tc9",
        state=ThreadJob.State.PENDING,
    )
    # Backdate to before the threshold (cron runs every 15 min;
    # STALE_JOB_THRESHOLD is 1 hour per the plan).
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        created_at=timezone.now() - timedelta(hours=2)
    )

    with patch("apps.workspaces.tasks._procrastinate_job_active",
               new=AsyncMock(return_value=False)), \
         patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume:
        resume.defer_async = AsyncMock(return_value=None)
        result = await expire_stale_thread_jobs()

    # The janitor defers the resume task — state is NOT flipped by the janitor.
    # The resume task is responsible for that transition.
    resume.defer_async.assert_awaited_once_with(thread_job_id=str(tj.id))
    assert result == {"flipped": 1}
    await sync_to_async(tj.refresh_from_db)()
    assert tj.state == ThreadJob.State.PENDING


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_janitor_fallback_flips_to_failed_when_defer_raises():
    """If the defer itself fails, the janitor falls back to flipping FAILED
    directly so the user doesn't see a permanent spinner."""
    user = await sync_to_async(User.objects.create_user)(email="b@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W2", created_by=user)
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=8888, tool_call_id="tc8",
        state=ThreadJob.State.PENDING,
    )
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        created_at=timezone.now() - timedelta(hours=2)
    )

    with patch("apps.workspaces.tasks._procrastinate_job_active",
               new=AsyncMock(return_value=False)), \
         patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume:
        resume.defer_async = AsyncMock(side_effect=RuntimeError("queue unavailable"))
        result = await expire_stale_thread_jobs()

    # flipped count is 0 because defer failed (fallback path doesn't increment)
    assert result == {"flipped": 0}
    await sync_to_async(tj.refresh_from_db)()
    # Fallback: janitor flips to FAILED so the user doesn't see a spinner forever.
    assert tj.state == ThreadJob.State.FAILED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_janitor_marks_stuck_running_failed_without_deferring_resume():
    """A ThreadJob stuck in RUNNING (worker crashed mid-ainvoke) is marked
    FAILED directly — the janitor does NOT defer a duplicate resume which
    could race with the original."""
    user = await sync_to_async(User.objects.create_user)(email="stuck@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W-stuck", created_by=user)
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=12345, tool_call_id="tc-stuck",
        state=ThreadJob.State.RUNNING,
    )
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        created_at=timezone.now() - timedelta(hours=2),
    )

    with patch("apps.workspaces.tasks._procrastinate_job_active",
               new=AsyncMock(return_value=False)), \
         patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume:
        resume.defer_async = AsyncMock(return_value=None)
        result = await expire_stale_thread_jobs()

    resume.defer_async.assert_not_called()
    await sync_to_async(tj.refresh_from_db)()
    assert tj.state == ThreadJob.State.FAILED
    assert tj.completed_at is not None
    assert result["flipped"] == 1
