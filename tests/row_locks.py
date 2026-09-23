"""Hold a competing row lock on a worker thread, for lock-wait tests."""

import asyncio
import contextlib
import threading

from django.contrib.auth import get_user_model
from django.db import connection, transaction

from apps.users.models import VerificationControl

HOLD_LIMIT_SECONDS = 10
JOIN_TIMEOUT_SECONDS = 5


def user_row(user_id):
    return lambda: get_user_model().objects.select_for_update().get(pk=user_id)


def control_row(connection_id):
    return lambda: VerificationControl.objects.select_for_update().get(connection_id=connection_id)


class _Holder:
    """Owns the holder thread; the waits are plain callables so the async variant
    can run them off the event loop without duplicating the orchestration."""

    def __init__(self, lock, release, release_after, acquire_timeout):
        self.lock = lock
        self.release = release or threading.Event()
        self.acquired = threading.Event()
        self.acquire_timeout = acquire_timeout
        self.outcome = {}
        self.timer = (
            threading.Timer(release_after, self.release.set) if release_after is not None else None
        )
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            with transaction.atomic():
                # Bound acquisition as well: a row already locked elsewhere must fail
                # fast with the real error, not park this thread past every join.
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('lock_timeout', %s, true)",
                        [f"{int(self.acquire_timeout * 1000)}ms"],
                    )
                self.lock()
                self.acquired.set()
                self.outcome["released"] = self.release.wait(timeout=HOLD_LIMIT_SECONDS)
        except Exception as exc:
            self.outcome["error"] = exc
        finally:
            connection.close()

    def wait_acquired(self):
        return self.acquired.wait(timeout=self.acquire_timeout)

    def on_acquire_result(self, acquired):
        if not acquired:
            raise AssertionError("lock holder never acquired the row") from self.outcome.get(
                "error"
            )
        if self.timer is not None:
            self.timer.start()

    def stop(self):
        self.release.set()
        if self.timer is not None:
            self.timer.cancel()

    def join(self):
        self.thread.join(timeout=JOIN_TIMEOUT_SECONDS)

    def check(self):
        assert not self.thread.is_alive(), "lock holder did not finish"
        if "error" in self.outcome:
            raise AssertionError("lock holder failed") from self.outcome["error"]
        assert self.outcome.get("released"), "lock holder hit its hold limit"


@contextlib.contextmanager
def row_locked(lock, *, release=None, release_after=None, acquire_timeout=2.0):
    """Hold ``lock()`` inside a transaction on another thread; yield the release event.

    Release is unconditional on exit: a failed assertion in the body would otherwise
    leave the holder's lock in place for the full hold limit, and under
    ``transaction=True`` the teardown TRUNCATE blocks behind it, cascading one
    failure into its neighbours. The holder records its outcome for the test thread
    to assert on, because an assert on the holder's own thread is invisible to pytest.
    """
    holder = _Holder(lock, release, release_after, acquire_timeout)
    holder.thread.start()
    try:
        holder.on_acquire_result(holder.wait_acquired())
        yield holder.release
    finally:
        holder.stop()
        holder.join()
    holder.check()


@contextlib.asynccontextmanager
async def arow_locked(lock, *, release=None, release_after=None, acquire_timeout=2.0):
    """:func:`row_locked` for async tests: the blocking waits run off the event loop."""
    holder = _Holder(lock, release, release_after, acquire_timeout)
    holder.thread.start()
    try:
        holder.on_acquire_result(await asyncio.to_thread(holder.wait_acquired))
        yield holder.release
    finally:
        holder.stop()
        await asyncio.to_thread(holder.join)
    holder.check()
