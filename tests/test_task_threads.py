import asyncio
import contextlib
import threading
import time

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

    async with asyncio.timeout(5):
        (a1, a2), (b1, b2) = await asyncio.gather(run(), run())

    assert a1 == a2
    assert b1 == b2
    assert a1 != b1
    assert shared not in {a1, b1}


@pytest.mark.asyncio
async def test_job_exit_does_not_wait_for_a_sync_call_still_running():
    release = threading.Event()
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow():
        loop.call_soon_threadsafe(started.set)
        release.wait(3)

    calls = []

    async def job():
        calls.append(asyncio.ensure_future(sync_to_async(slow)()))
        await started.wait()
        calls[0].cancel()

    # A blocked loop can't time itself out, so measure instead of wait_for.
    began = time.monotonic()
    try:
        await isolate_job_thread(job, context=None, worker=None)
    finally:
        elapsed = time.monotonic() - began
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await calls[0]
    assert elapsed < 1
