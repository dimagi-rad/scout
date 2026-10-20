"""Give each Procrastinate job its own thread for thread-sensitive sync work.

Outside a request, asgiref runs every ``sync_to_async(thread_sensitive=True)``
call, and with it all async ORM, on one process-wide thread. With several jobs
running at once (#918), one job's long sync call (a canvas commit's Cube build,
~110s) would queue every other job's ORM behind it, and the jobs would share one
DB session, where ``pg_try_advisory_lock`` is re-entrant: two builds could both
take the single Cube validator slot. Django gives each ASGI request its own
context the same way.

Each job's thread opens its own Django connection. Procrastinate's DjangoApp task
middleware runs inside this context, so with CONN_MAX_AGE=0 it closes that
connection as the job ends.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from asgiref.sync import SyncToAsync, ThreadSensitiveContext

if TYPE_CHECKING:
    from procrastinate import job_context
    from procrastinate.middleware import AsyncCallNext
    from procrastinate.worker import Worker


async def isolate_job_thread(
    call_next: AsyncCallNext, context: job_context.JobContext, worker: Worker
) -> Any:
    # Not ``async with ThreadSensitiveContext()``: its exit shuts the executor down
    # with wait=True on the event loop, so a job cancelled twice mid sync call would
    # freeze the whole worker (other jobs, heartbeat) until that call returned.
    # The cost: such an orphaned call keeps running after its job has ended and
    # released its data locks, so a retry may overlap it.
    try:
        SyncToAsync.thread_sensitive_context.get()
    except LookupError:
        pass
    else:
        return await call_next()
    job_thread = ThreadSensitiveContext()
    token = SyncToAsync.thread_sensitive_context.set(job_thread)
    try:
        return await call_next()
    finally:
        SyncToAsync.thread_sensitive_context.reset(token)
        executor = SyncToAsync.context_to_thread_executor.pop(job_thread, None)
        if executor is not None:
            executor.shutdown(wait=False)
