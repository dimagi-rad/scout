"""Give each Procrastinate job its own thread for thread-sensitive sync work.

Outside a request, asgiref runs every ``sync_to_async(thread_sensitive=True)``
call, and with it all async ORM, on one process-wide thread. With several jobs
running at once (#918), one job's long sync call (a canvas commit's Cube build,
~110s) would queue every other job's ORM behind it. Django gives each ASGI
request its own context the same way.

Each job's thread opens its own Django connection; Procrastinate's DjangoApp
task middleware runs inside this context and closes it as the job ends.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from asgiref.sync import ThreadSensitiveContext

if TYPE_CHECKING:
    from procrastinate import job_context
    from procrastinate.middleware import AsyncCallNext
    from procrastinate.worker import Worker


async def isolate_job_thread(
    call_next: AsyncCallNext, context: job_context.JobContext, worker: Worker
) -> Any:
    async with ThreadSensitiveContext():
        return await call_next()
