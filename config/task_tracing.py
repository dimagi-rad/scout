"""Sentry performance tracing for Procrastinate jobs.

Without this the worker sends no transactions, so materialization and rebuild
timings are invisible (#918). ``trace_job`` is a worker middleware: procrastinate
runs it on the event loop around every job, sync and async alike, and asgiref
copies the context into a sync task's thread, so spans the task opens (DB,
HTTP) attach to the job's transaction.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import TYPE_CHECKING, Any

import sentry_sdk
from procrastinate.exceptions import JobAborted

if TYPE_CHECKING:
    from procrastinate import job_context
    from procrastinate.middleware import AsyncCallNext
    from procrastinate.worker import Worker

log = logging.getLogger(__name__)

# The most recent time the job became runnable: first deferral, a retry, or a
# manual retry from the admin. A later ``scheduled_at`` wins (see queue_wait_seconds).
_LAST_QUEUED_AT = """
SELECT max(at) AS queued_at
FROM procrastinate_events
WHERE job_id = %(job_id)s AND type IN ('deferred', 'deferred_for_retry', 'retried')
"""


def queue_wait_seconds(
    queued_at: datetime.datetime | None,
    scheduled_at: datetime.datetime | None,
    started_at: float,
) -> float | None:
    """Seconds the job sat runnable before a worker picked it up.

    A job deferred to run later only starts waiting at ``scheduled_at``.
    """
    ready_at = max((t for t in (queued_at, scheduled_at) if t is not None), default=None)
    if ready_at is None:
        return None
    return max(0.0, started_at - ready_at.timestamp())


async def _fetch_queued_at(worker: Worker, job_id: int) -> datetime.datetime | None:
    try:
        row = await worker.app.connector.execute_query_one_async(_LAST_QUEUED_AT, job_id=job_id)
    except Exception:  # Timing is best-effort; never fail a job over it.
        log.debug("Could not read queue time for job %s", job_id, exc_info=True)
        return None
    return row["queued_at"] if row else None


def _status(error: BaseException | None) -> str:
    if error is None:
        return "ok"
    if isinstance(error, (JobAborted, asyncio.CancelledError)):
        return "cancelled"
    return "internal_error"


async def trace_job(
    call_next: AsyncCallNext, context: job_context.JobContext, worker: Worker
) -> Any:
    job = context.job
    result = error = None
    # The error is re-raised only after the spans close: their __exit__ would
    # mark an aborted or cancelled job as internal_error.
    with (
        sentry_sdk.isolation_scope(),
        sentry_sdk.start_transaction(
            op="queue.task", name=job.task_name, source="task"
        ) as transaction,
    ):
        transaction.set_tag("procrastinate.queue", job.queue)
        transaction.set_data("procrastinate.attempts", job.attempts)
        wait = None
        # Only sampled transactions pay for the extra query.
        if transaction.sampled and job.id is not None:
            queued_at = await _fetch_queued_at(worker, job.id)
            wait = queue_wait_seconds(queued_at, job.scheduled_at, context.start_timestamp)
            if wait is not None:
                transaction.set_data("queue.wait_ms", round(wait * 1000))
        # Sentry's Queues view reads consumer timings from a ``queue.process`` span.
        with sentry_sdk.start_span(op="queue.process", name=job.task_name) as span:
            span.set_data("messaging.system", "procrastinate")
            span.set_data("messaging.destination.name", job.queue)
            span.set_data("messaging.message.id", str(job.id))
            span.set_data("messaging.message.retry.count", job.attempts)
            if wait is not None:
                span.set_data("messaging.message.receive.latency", round(wait * 1000))
            try:
                result = await call_next()
            except BaseException as exc:
                error = exc
            span.set_status(_status(error))
        transaction.set_status(_status(error))
    if error is not None:
        raise error
    return result
