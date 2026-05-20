"""Shared cancel logic for ThreadJob + the materialization runs it owns.

Both the per-job cancel endpoint and the legacy materialization cancel endpoint
funnel through ``cancel_thread_job`` so the order-of-operations (DB state flip
BEFORE procrastinate abort) stays correct.
"""

import logging
from datetime import UTC, datetime

from procrastinate.contrib.django.procrastinate_app import current_app

from apps.chat.models import ThreadJob
from apps.workspaces.models import MaterializationRun

logger = logging.getLogger(__name__)


async def cancel_thread_job(thread_job: ThreadJob) -> int:
    """Cancel the given ThreadJob and its associated MaterializationRuns.

    Returns the number of MaterializationRun rows flipped to CANCELLED.

    Order matters: DB state is flipped before the procrastinate abort signal,
    because the worker's progress_updater checks DB state on every page and
    procrastinate's abort only fires at the next ``await`` boundary.
    """
    now = datetime.now(UTC)

    runs_cancelled = await MaterializationRun.objects.filter(
        procrastinate_job_id=thread_job.procrastinate_job_id,
        state__in=list(MaterializationRun.ACTIVE_STATES),
    ).aupdate(
        state=MaterializationRun.RunState.CANCELLED,
        completed_at=now,
    )

    await ThreadJob.objects.filter(id=thread_job.id).aupdate(
        state=ThreadJob.State.CANCELLED,
        completed_at=now,
    )

    try:
        await current_app.job_manager.cancel_job_by_id_async(
            thread_job.procrastinate_job_id, abort=True
        )
    except Exception:
        logger.warning(
            "Failed to abort procrastinate job %s",
            thread_job.procrastinate_job_id,
            exc_info=True,
        )

    return runs_cancelled
