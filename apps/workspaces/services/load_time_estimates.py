"""Bounded successful workspace-load history and approximate progress timing."""

import logging
from statistics import median

from django.core.cache import cache
from django.db.models import F
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.telemetry import recorder
from apps.telemetry.models import EventKind, Outcome
from apps.workspaces.models import MaterializationRun, WorkspaceLoadTiming
from apps.workspaces.task_dispatch import MATERIALIZE_WORKSPACE

logger = logging.getLogger(__name__)
HISTORY_LIMIT = 10
HISTORY_CACHE_SECONDS = 60


def _cache_key(workspace_id, only_unserved):
    return f"load_time_history:{workspace_id}:{int(only_unserved)}"


def _close_phase(timing, now):
    durations = dict(timing.phase_seconds)
    seconds = max(0, (now - timing.phase_started_at).total_seconds())
    durations[timing.phase] = durations.get(timing.phase, 0) + seconds
    return durations


async def astart_load_timing(workspace_id, job_id, *, only_unserved=False):
    try:
        # A retried queue job starts a new attempt; failed attempts are never samples.
        await WorkspaceLoadTiming.objects.aupdate_or_create(
            job_id=job_id,
            defaults={
                "workspace_id": workspace_id,
                "only_unserved": only_unserved,
                "started_at": timezone.now(),
                "completed_at": None,
                "succeeded": False,
                "phase": "loading",
                "phase_started_at": timezone.now(),
                "phase_seconds": {},
            },
        )
    except Exception:
        logger.warning("Could not start load timing", exc_info=True)


async def atransition_load_phase(job_id, phase, *, now=None):
    if job_id is None:
        return
    try:
        timing = await WorkspaceLoadTiming.objects.filter(job_id=job_id, completed_at=None).afirst()
        if timing is None or timing.phase == phase:
            return
        now = now or timezone.now()
        await WorkspaceLoadTiming.objects.filter(pk=timing.pk).aupdate(
            phase=phase,
            phase_started_at=now,
            phase_seconds=_close_phase(timing, now),
        )
    except Exception:
        logger.warning("Could not record load phase timing", exc_info=True)


def transition_load_phase(job_id, phase):
    if job_id is None:
        return
    try:
        timing = WorkspaceLoadTiming.objects.filter(job_id=job_id, completed_at=None).first()
        if timing is None or timing.phase == phase:
            return
        now = timezone.now()
        WorkspaceLoadTiming.objects.filter(pk=timing.pk).update(
            phase=phase,
            phase_started_at=now,
            phase_seconds=_close_phase(timing, now),
        )
    except Exception:
        logger.warning("Could not record load phase timing", exc_info=True)


async def afinish_load_timing(job_id, succeeded, *, now=None, require_runs=False):
    timing = None
    finished = 0
    try:
        if succeeded and require_runs:
            runs = MaterializationRun.objects.filter(procrastinate_job_id=job_id)
            succeeded = (
                await runs.aexists()
                and not await runs.exclude(
                    state=MaterializationRun.RunState.COMPLETED,
                    completed_at__isnull=False,
                ).aexists()
            )
        timing = await WorkspaceLoadTiming.objects.filter(job_id=job_id, completed_at=None).afirst()
        if timing is None:
            return
        now = now or timezone.now()
        phase_seconds = _close_phase(timing, now)
        finished = await WorkspaceLoadTiming.objects.filter(
            pk=timing.pk, completed_at=None
        ).aupdate(
            completed_at=now,
            succeeded=succeeded,
            phase_seconds=phase_seconds,
        )
        await cache.adelete(_cache_key(timing.workspace_id, timing.only_unserved))
    except Exception:
        logger.warning("Could not finish load timing", exc_info=True)
    # Only the finisher that closed the row records it, and a failed cache clear
    # after that does not lose the event.
    if timing is None or not finished:
        return
    try:
        phase_ms = {
            f"{phase}_ms": round(seconds * 1000) for phase, seconds in phase_seconds.items()
        }
    except (TypeError, ValueError):
        logger.warning("Load timing for job %s has a non-numeric phase", job_id)
        phase_ms = {}
    await recorder.arecord(
        EventKind.WORKSPACE_LOAD,
        workspace_id=timing.workspace_id,
        outcome=Outcome.COMPLETED if succeeded else Outcome.FAILED,
        duration_ms=(now - timing.started_at).total_seconds() * 1000,
        occurred_at=now,
        attrs={
            "only_unserved": timing.only_unserved,
            "job_id": job_id,
            **phase_ms,
        },
    )


async def _history(workspace_id, only_unserved):
    key = _cache_key(workspace_id, only_unserved)
    cached = await cache.aget(key)
    if cached is not None:
        return cached
    samples = [
        (row.started_at, (row.completed_at - row.started_at).total_seconds(), row.phase_seconds)
        async for row in WorkspaceLoadTiming.objects.filter(
            workspace_id=workspace_id,
            only_unserved=only_unserved,
            succeeded=True,
            completed_at__gt=F("started_at"),
        ).order_by("-started_at")[:HISTORY_LIMIT]
    ]
    await cache.aset(key, samples, HISTORY_CACHE_SECONDS)
    return samples


def _estimate(samples, current, now):
    if not samples:
        return None
    phases = {}
    for _, _, durations in samples:
        for phase, seconds in durations.items():
            if seconds > 0:
                phases.setdefault(phase, []).append(seconds)
    return {
        "usual_seconds": median(sample[1] for sample in samples),
        "elapsed_seconds": max(
            0, ((current.completed_at or now) - current.started_at).total_seconds()
        )
        if current
        else None,
        "sample_count": len(samples),
        "phase_seconds": {phase: median(seconds) for phase, seconds in phases.items()},
    }


async def aload_time_estimates(workspace_id, job_ids, *, now=None):
    if not job_ids:
        return {}
    current_by_job = {
        timing.job_id: timing
        async for timing in WorkspaceLoadTiming.objects.filter(
            workspace_id=workspace_id,
            job_id__in=job_ids,
        )
    }
    mode_by_job = {job_id: timing.only_unserved for job_id, timing in current_by_job.items()}
    missing = set(job_ids) - current_by_job.keys()
    if missing:
        async for job_id, mode in ProcrastinateJob.objects.filter(
            id__in=missing,
            task_name=MATERIALIZE_WORKSPACE,
            args__workspace_id=str(workspace_id),
        ).values_list("id", "args__only_unserved"):
            mode_by_job[job_id] = bool(mode)
    histories = {mode: await _history(workspace_id, mode) for mode in set(mode_by_job.values())}
    now = now or timezone.now()
    return {
        job_id: _estimate(
            histories.get(mode_by_job.get(job_id), []),
            current_by_job.get(job_id),
            now,
        )
        for job_id in job_ids
    }


async def aload_time_estimate(workspace_id, job_id, *, now=None):
    return (await aload_time_estimates(workspace_id, {job_id}, now=now))[job_id]
