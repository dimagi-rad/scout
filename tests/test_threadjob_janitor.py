from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone
from langchain_core.messages import AIMessage

from apps.chat.models import Thread, ThreadJob
from apps.workspaces.models import Workspace
from apps.workspaces.tasks import (
    RESUME_STUCK_RUNNING_MESSAGE,
    STALE_JOB_THRESHOLD,
    _procrastinate_job_status,
    expire_stale_thread_jobs,
    reconcile_stale_thread_job,
)

User = get_user_model()


@pytest.fixture
def procrastinate_job():
    """Factory that inserts real rows into the unmanaged procrastinate_jobs
    table and cleans them up on teardown.

    The Django contrib ProcrastinateJob model is read-only (managed=False), so
    rows are inserted via raw SQL (status/queue_name/task_name are the only
    NOT NULL columns without a default; status is what the janitor reads).

    Explicit cleanup is required: with django_db(transaction=True), Django's
    teardown TRUNCATEs tables with data, and the procrastinate_jobs FK graph
    breaks the non-CASCADE truncate ordering — leaving stray rows there
    poisons the connection for subsequent tests. We delete what we inserted.
    """
    created: list[int] = []

    def _make(status: str) -> int:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, status) "
                "VALUES (%s, %s, %s::procrastinate_job_status) RETURNING id",
                ["default", "test_task", status],
            )
            job_id = cursor.fetchone()[0]
        created.append(job_id)
        return job_id

    yield _make

    if created:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM procrastinate_jobs WHERE id = ANY(%s)",
                [created],
            )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_janitor_defers_resume_for_stale_threadjobs():
    """Janitor defers the resume task first and leaves state unchanged.
    The resume task is responsible for flipping the ThreadJob state so the
    user gets an agent follow-up message instead of a silent FAILED."""
    user = await User.objects.acreate_user(email="a@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=9999,
        tool_call_id="tc9",
        state=ThreadJob.State.PENDING,
    )
    # Backdate to before the threshold (cron runs every 15 min;
    # STALE_JOB_THRESHOLD is 1 hour per the plan).
    await ThreadJob.objects.filter(id=tj.id).aupdate(created_at=timezone.now() - timedelta(hours=2))

    with (
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="succeeded"),
        ),
        patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume,
    ):
        resume.defer_async = AsyncMock(return_value=None)
        result = await expire_stale_thread_jobs()

    # The janitor defers the resume task — state is NOT flipped by the janitor.
    # The resume task is responsible for that transition.
    resume.defer_async.assert_awaited_once_with(thread_job_id=str(tj.id))
    assert result == {"flipped": 1}
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.PENDING


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_janitor_fallback_flips_to_failed_when_defer_raises():
    """If the defer itself fails, the janitor falls back to flipping FAILED
    directly so the user doesn't see a permanent spinner."""
    user = await User.objects.acreate_user(email="b@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W2", created_by=user)
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=8888,
        tool_call_id="tc8",
        state=ThreadJob.State.PENDING,
    )
    await ThreadJob.objects.filter(id=tj.id).aupdate(created_at=timezone.now() - timedelta(hours=2))

    with (
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="succeeded"),
        ),
        patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume,
    ):
        resume.defer_async = AsyncMock(side_effect=RuntimeError("queue unavailable"))
        result = await expire_stale_thread_jobs()

    # flipped count is 0 because defer failed (fallback path doesn't increment)
    assert result == {"flipped": 0}
    await tj.arefresh_from_db()
    # Fallback: janitor flips to FAILED so the user doesn't see a spinner forever.
    assert tj.state == ThreadJob.State.FAILED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_janitor_skips_threadjob_when_procrastinate_status_unknown():
    """Finding #10: when _procrastinate_job_status returns None (status check
    failed, e.g. a transient DB blip), the janitor must NOT touch the
    ThreadJob. The previous bare ``except → return False`` conflated
    "not active" with "couldn't tell" — the janitor would then incorrectly
    clean up actively-running jobs during an outage."""
    user = await User.objects.acreate_user(email="unknown@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-unknown", created_by=user)
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=24242,
        tool_call_id="tc-unknown",
        state=ThreadJob.State.PENDING,
    )
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        created_at=timezone.now() - timedelta(hours=2),
    )

    with (
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value=None),  # status unknown
        ),
        patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume,
    ):
        resume.defer_async = AsyncMock(return_value=None)
        result = await expire_stale_thread_jobs()

    # No resume deferred and no state change — the row is left for the next
    # tick to retry.
    resume.defer_async.assert_not_called()
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.PENDING
    assert result == {"flipped": 0}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_janitor_marks_stuck_running_failed_without_deferring_resume():
    """A ThreadJob stuck in RUNNING (worker crashed mid-ainvoke) is marked
    FAILED directly — the janitor does NOT defer a duplicate resume which
    could race with the original."""
    user = await User.objects.acreate_user(email="stuck@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-stuck", created_by=user)
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=12345,
        tool_call_id="tc-stuck",
        state=ThreadJob.State.RUNNING,
    )
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        created_at=timezone.now() - timedelta(hours=2),
    )

    with (
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="succeeded"),
        ),
        patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume,
        patch(
            "apps.workspaces.tasks._persist_synthetic_failure_message",
            new=AsyncMock(return_value=None),
        ),
    ):
        resume.defer_async = AsyncMock(return_value=None)
        result = await expire_stale_thread_jobs()

    resume.defer_async.assert_not_called()
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.FAILED
    assert tj.completed_at is not None
    assert result["flipped"] == 1
    # Stuck-RUNNING flip writes a generic, user-facing error_summary so the
    # frontend's failure card has a message instead of a blank header.
    assert tj.error_summary
    assert "restart" in tj.error_summary.lower() or "interrupted" in tj.error_summary.lower()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_janitor_persists_synthetic_message_on_stuck_running():
    """When the janitor flips a stuck RUNNING ThreadJob to FAILED, it must
    also persist a user-visible AIMessage in the chat so the user sees a
    clear "your follow-up was interrupted" message instead of a spinner that
    silently disappears."""
    user = await User.objects.acreate_user(email="ghost@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-ghost", created_by=user)
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=54321,
        tool_call_id="tc-ghost",
        state=ThreadJob.State.RUNNING,
    )
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        created_at=timezone.now() - STALE_JOB_THRESHOLD - timedelta(minutes=5),
    )

    mock_agent = MagicMock()
    mock_agent.aupdate_state = AsyncMock(return_value=None)

    with (
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="succeeded"),
        ),
        patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume,
        patch(
            "apps.workspaces.tasks._build_agent_for_resume",
            new=AsyncMock(return_value=mock_agent),
        ),
    ):
        resume.defer_async = AsyncMock(return_value=None)
        result = await expire_stale_thread_jobs()

    assert result["flipped"] == 1
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.FAILED

    # The synthetic AIMessage made it into the LangGraph state, not just the logs.
    mock_agent.aupdate_state.assert_awaited()
    msg = mock_agent.aupdate_state.await_args.args[1]["messages"][0]
    assert isinstance(msg, AIMessage)
    assert msg.content == RESUME_STUCK_RUNNING_MESSAGE


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_reconciler_does_not_fail_healthy_long_running_resume():
    """Finding 02#9: a healthy long-running materialization (>10 min) must NOT
    be flipped to FAILED once its resume task starts running.

    The bug: staleness was measured from ``created_at`` (materialize dispatch)
    and the MATERIALIZE job's status was inspected. For any materialization
    taking >10 min, the moment the resume claims the job (RUNNING) and enters
    agent.ainvoke, the materialize job is already 'succeeded' and the old
    reconciler CAS-flipped the still-live resume to FAILED, injecting a
    synthetic 'interrupted' message that raced the real answer.

    Fix: staleness for a RUNNING job is keyed off the RESUME phase
    (``started_at``), not ``created_at``. A resume that started seconds ago is
    NOT stale even if the materialization began an hour earlier."""
    user = await User.objects.acreate_user(email="longmat@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-longmat", created_by=user)
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=606060,
        tool_call_id="tc-longmat",
        state=ThreadJob.State.RUNNING,
    )
    # Materialization dispatched well over the threshold ago (a long pipeline),
    # but the resume claimed the job and started just now.
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        created_at=timezone.now() - timedelta(hours=2),
        started_at=timezone.now(),
    )
    await tj.arefresh_from_db()

    with (
        # The MATERIALIZE job has long since 'succeeded' — the old reconciler
        # keyed off this and wrongly flipped the live resume.
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="succeeded"),
        ),
        patch(
            "apps.workspaces.tasks._persist_synthetic_failure_message",
            new=AsyncMock(return_value=None),
        ) as persist,
    ):
        action = await reconcile_stale_thread_job(tj)

    # The healthy in-flight resume must be left alone.
    assert action is None, f"healthy long-running resume must not be reconciled; got {action!r}"
    persist.assert_not_called()
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.RUNNING


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_reconciler_fails_genuinely_stuck_running_resume():
    """The flip-side of 02#9: a resume that started long ago (the worker crashed
    mid-ainvoke) IS stale by its RESUME phase and must still be flipped to
    FAILED so the user is not stuck on a permanent spinner."""
    user = await User.objects.acreate_user(email="stuckresume@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-stuckresume", created_by=user)
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=707070,
        tool_call_id="tc-stuckresume",
        state=ThreadJob.State.RUNNING,
    )
    # The resume itself started well over the threshold ago and never finished.
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        created_at=timezone.now() - timedelta(hours=2),
        started_at=timezone.now() - STALE_JOB_THRESHOLD - timedelta(minutes=5),
    )
    await tj.arefresh_from_db()

    with (
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="succeeded"),
        ),
        patch(
            "apps.workspaces.tasks._persist_synthetic_failure_message",
            new=AsyncMock(return_value=None),
        ),
    ):
        action = await reconcile_stale_thread_job(tj)

    assert action == "failed"
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.FAILED


def test_janitor_cutoff_is_10_minutes():
    """STALE_JOB_THRESHOLD is the contract with frontend UX: how long before a
    user sees a synthetic 'we interrupted you' message. Locking the value in a
    test makes any future tightening intentional rather than accidental."""
    assert timedelta(minutes=10) == STALE_JOB_THRESHOLD


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_janitor_fallback_failed_writes_error_summary():
    """The defer-fail fallback also writes a user-facing error_summary."""
    user = await User.objects.acreate_user(email="fb@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-fb", created_by=user)
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=77777,
        tool_call_id="tc-fb",
        state=ThreadJob.State.PENDING,
    )
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        created_at=timezone.now() - timedelta(hours=2),
    )

    with (
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="succeeded"),
        ),
        patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume,
    ):
        resume.defer_async = AsyncMock(side_effect=RuntimeError("queue down"))
        await expire_stale_thread_jobs()

    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.FAILED
    assert tj.error_summary
    assert "queue" in tj.error_summary.lower() or "retry" in tj.error_summary.lower()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_janitor_flips_pending_failed_job_directly_without_resume():
    """When the procrastinate job itself FAILED (task raised — e.g. the
    worker's DB connection died before any pipeline ran), there is no result
    for an agent resume to narrate. The janitor flips the ThreadJob straight
    to FAILED with a user-facing error_summary and a synthetic chat message,
    and does NOT defer a resume."""
    user = await User.objects.acreate_user(email="pf@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-pf", created_by=user)
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=31337,
        tool_call_id="tc-pf",
        state=ThreadJob.State.PENDING,
    )
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        created_at=timezone.now() - timedelta(hours=2),
    )

    with (
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="failed"),
        ),
        patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume,
        patch(
            "apps.workspaces.tasks._persist_synthetic_failure_message",
            new=AsyncMock(return_value=None),
        ) as persist,
    ):
        resume.defer_async = AsyncMock(return_value=None)
        result = await expire_stale_thread_jobs()

    resume.defer_async.assert_not_called()
    persist.assert_awaited_once()
    assert result["flipped"] == 1
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.FAILED
    assert tj.completed_at is not None
    assert tj.error_summary


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_procrastinate_job_status_reads_status_from_orm_row(procrastinate_job):
    """_procrastinate_job_status reads the raw status string from the
    procrastinate_jobs table via the Django contrib ORM model — not via the
    unresolved FutureApp proxy, which raised AttributeError on every call in
    the worker and silently disabled the janitor."""
    failed_id = await sync_to_async(procrastinate_job)("failed")
    assert await _procrastinate_job_status(failed_id) == "failed"

    doing_id = await sync_to_async(procrastinate_job)("doing")
    assert await _procrastinate_job_status(doing_id) == "doing"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_procrastinate_job_status_returns_none_for_missing_job():
    """A job id with no matching procrastinate_jobs row returns None (the
    "unknown — don't touch this row" sentinel), not an exception."""
    assert await _procrastinate_job_status(999_999_999) is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_janitor_reconciles_stale_job_against_real_failed_procrastinate_row(
    procrastinate_job,
):
    """End-to-end: a stale PENDING ThreadJob backed by a real procrastinate
    job in 'failed' state is flipped to FAILED by the janitor using the
    ORM-based status lookup (no mock of _procrastinate_job_status). This is the
    exact scenario the FutureApp bug was suppressing for the zombie ThreadJobs."""
    job_id = await sync_to_async(procrastinate_job)("failed")
    user = await User.objects.acreate_user(email="zombie@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-zombie", created_by=user)
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=job_id,
        tool_call_id="tc-zombie",
        state=ThreadJob.State.PENDING,
    )
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        created_at=timezone.now() - timedelta(hours=2),
    )

    with (
        patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume,
        patch(
            "apps.workspaces.tasks._persist_synthetic_failure_message",
            new=AsyncMock(return_value=None),
        ),
    ):
        resume.defer_async = AsyncMock(return_value=None)
        result = await expire_stale_thread_jobs()

    # Failed procrastinate job -> ThreadJob flipped straight to FAILED, no resume.
    resume.defer_async.assert_not_called()
    assert result["flipped"] == 1
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.FAILED
    assert tj.completed_at is not None
    assert tj.error_summary
