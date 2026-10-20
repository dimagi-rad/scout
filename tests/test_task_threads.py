import asyncio
import threading

import pytest
from asgiref.sync import sync_to_async
from django.conf import settings

from config.task_threads import isolate_job_thread


def test_every_worker_job_gets_its_own_thread_context():
    assert isolate_job_thread in settings.PROCRASTINATE_WORKER_DEFAULTS["worker_middleware"]


@pytest.mark.asyncio
async def test_concurrent_jobs_do_not_share_the_thread_sensitive_thread():
    # Both job threads stay alive until each has run, so their idents can't be reused.
    both_ran = asyncio.Barrier(2)
    shared = await sync_to_async(threading.get_ident)()

    async def job():
        first = await sync_to_async(threading.get_ident)()
        await both_ran.wait()
        second = await sync_to_async(threading.get_ident)()
        return first, second

    async def run():
        return await isolate_job_thread(job, context=None, worker=None)

    (a1, a2), (b1, b2) = await asyncio.gather(run(), run())

    assert a1 == a2
    assert b1 == b2
    assert a1 != b1
    assert shared not in {a1, b1}
