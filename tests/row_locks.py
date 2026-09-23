"""Hold a competing row lock on a worker thread, for lock-wait tests."""

import asyncio
import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor

from django.contrib.auth import get_user_model
from django.db import connection, transaction

from apps.users.models import VerificationControl

HOLD_LIMIT_SECONDS = 10


def user_row(user_id):
    return lambda: get_user_model().objects.select_for_update().get(pk=user_id)


def control_row(connection_id):
    return lambda: VerificationControl.objects.select_for_update().get(connection_id=connection_id)


def _hold(lock, acquired, release):
    try:
        with transaction.atomic():
            lock()
            acquired.set()
            return release.wait(timeout=HOLD_LIMIT_SECONDS)
    finally:
        connection.close()


@contextlib.contextmanager
def row_locked(lock, *, release=None, release_after=None, acquire_timeout=2.0):
    """Hold ``lock()`` inside a transaction on another thread; yield the release event.

    Release is unconditional on exit: a failed assertion in the body would otherwise
    leave the holder's lock in place for the full hold limit, and under
    ``transaction=True`` the teardown TRUNCATE blocks behind it, cascading one
    failure into its neighbours. The holder reports through its future rather than
    asserting on its own thread, where pytest cannot see a failure.
    """
    release = release or threading.Event()
    acquired = threading.Event()
    timer = threading.Timer(release_after, release.set) if release_after is not None else None
    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(_hold, lock, acquired, release)
        try:
            if not acquired.wait(timeout=acquire_timeout):
                if holder.done():
                    holder.result()
                raise AssertionError("lock holder never acquired the row")
            if timer is not None:
                timer.start()
            yield release
        finally:
            release.set()
            if timer is not None:
                timer.cancel()
        assert holder.result(timeout=acquire_timeout), "lock holder hit its hold limit"


@contextlib.asynccontextmanager
async def arow_locked(lock, *, release=None, release_after=None, acquire_timeout=2.0):
    """:func:`row_locked` for async tests: waits happen off the event loop."""
    release = release or threading.Event()
    acquired = threading.Event()
    timer = threading.Timer(release_after, release.set) if release_after is not None else None
    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(_hold, lock, acquired, release)
        try:
            if not await asyncio.to_thread(acquired.wait, acquire_timeout):
                if holder.done():
                    holder.result()
                raise AssertionError("lock holder never acquired the row")
            if timer is not None:
                timer.start()
            yield release
        finally:
            release.set()
            if timer is not None:
                timer.cancel()
        assert await asyncio.to_thread(holder.result, acquire_timeout), (
            "lock holder hit its hold limit"
        )
