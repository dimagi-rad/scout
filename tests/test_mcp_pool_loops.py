"""Managed-DB pools must never cross event loops (FOLLOW-UPS #3).

An ``AsyncConnectionPool``'s workers are tasks on the loop that opened it. When
that loop ends the pool is not ``closed``, but nothing services it: handing it to
another loop fails every checkout that needs a new connection. That is how one
worker-thread query used to cascade through the rest of a test run. These tests
run against a real managed PostgreSQL.
"""

from __future__ import annotations

import asyncio
import gc
import os
import threading
import time
import uuid

import psycopg
import pytest

from mcp_server.context import _parse_db_url
from mcp_server.services import pool as pool_mod

pytestmark = pytest.mark.skipif(
    not os.environ.get("MANAGED_DATABASE_URL"), reason="MANAGED_DATABASE_URL not set"
)


_APP_NAME = f"test_pool_loops_{uuid.uuid4().hex[:12]}"


@pytest.fixture(autouse=True)
def _tag_pool_connections(monkeypatch):
    """Tag this module's pooled connections so counts ignore every other client."""
    base = pool_mod._base_conninfo
    monkeypatch.setattr(
        pool_mod, "_base_conninfo", lambda params: f"{base(params)} application_name='{_APP_NAME}'"
    )


def _params():
    return _parse_db_url(os.environ["MANAGED_DATABASE_URL"], "public")


async def _select_one():
    pool = await pool_mod.get_pool(_params())
    async with pool.connection(timeout=5) as conn:
        cur = await conn.execute("SELECT 1")
        assert await cur.fetchone() == (1,)
    return pool


async def _hold_connections(n: int):
    """Check out ``n`` connections at once, so the pool must grow via its workers."""
    pool = await pool_mod.get_pool(_params())
    ready = asyncio.Barrier(n)

    async def one():
        async with pool.connection(timeout=3) as conn:
            await conn.execute("SELECT 1")
            await ready.wait()

    await asyncio.gather(*(one() for _ in range(n)))
    return pool


def _run_in_fresh_loop(coro_fn, *args):
    """What ``async_to_sync`` does from a plain worker thread: a new loop per call."""
    return asyncio.run(coro_fn(*args))


def _backend_connections() -> int:
    params = _params()
    params.pop("options", None)
    with psycopg.connect(**params, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s", (_APP_NAME,)
        ).fetchone()
    return row[0]


def _settled_backend_connections(timeout: float = 2.0) -> int:
    """A backend leaves pg_stat_activity shortly after its client disconnects."""
    deadline = time.monotonic() + timeout
    while (count := _backend_connections()) and time.monotonic() < deadline:
        time.sleep(0.05)
    return count


def _cached_pool_count() -> int:
    # Never compare _pools itself in an assert: its keys carry the DSN password.
    return len(pool_mod._pools)


def test_a_finished_loops_pool_is_never_handed_to_a_later_loop():
    """The cascade: a later loop got the dead loop's still-"open" pool and timed out."""
    first = _run_in_fresh_loop(_select_one)

    second = _run_in_fresh_loop(_hold_connections, 5)

    assert second is not first


def test_pools_are_closed_when_their_loop_ends():
    pool = _run_in_fresh_loop(_select_one)

    assert pool.closed
    assert _cached_pool_count() == 0


def test_two_live_loops_keep_their_own_pools():
    """Loop-tagged DSN keying made two live loops evict each other's open pools."""
    rounds = 3
    turn = threading.Barrier(2, timeout=10)
    release = threading.Event()
    seen: dict[str, list] = {"a": [], "b": []}
    cached_while_both_live: list[int] = []
    errors: list[BaseException] = []

    def live_loop(name):
        async def body():
            for _ in range(rounds):
                seen[name].append(await _select_one())
                await asyncio.to_thread(turn.wait)
            if name == "a":
                cached_while_both_live.append(_cached_pool_count())
            await asyncio.to_thread(release.wait, 10)

        try:
            asyncio.run(body())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=live_loop, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for _ in range(200):
        if cached_while_both_live or errors:
            break
        time.sleep(0.05)
    release.set()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive()

    assert errors == []
    assert cached_while_both_live == [2]
    assert len({id(p) for p in seen["a"]}) == 1
    assert len({id(p) for p in seen["b"]}) == 1
    assert seen["a"][0] is not seen["b"][0]
    assert all(p.closed for p in seen["a"] + seen["b"])


def test_connections_stay_bounded_across_many_sequential_loops():
    """Per-test (and per-async_to_sync) loops used to leak a pool each: CI hit
    ``too many clients already`` once loop-keyed caching stopped reusing them.
    35 loops x 3 connections is past PostgreSQL's default max_connections."""
    for _ in range(35):
        _run_in_fresh_loop(_hold_connections, 3)

    assert _settled_backend_connections() == 0
    assert _cached_pool_count() == 0


def test_live_pools_are_capped_and_a_new_loop_waits_for_a_slot(monkeypatch):
    monkeypatch.setattr(pool_mod, "_MAX_POOLS", 1)
    monkeypatch.setattr(pool_mod, "_SLOT_WAIT_SECONDS", 10.0)
    holder_ready = threading.Event()
    release_holder = threading.Event()

    async def hold_a_slot():
        await _select_one()
        holder_ready.set()
        await asyncio.to_thread(release_holder.wait, 10)

    holder = threading.Thread(target=_run_in_fresh_loop, args=(hold_a_slot,))
    holder.start()

    async def wait_then_release():
        waiter = asyncio.create_task(_select_one())
        await asyncio.sleep(0.3)
        assert not waiter.done()
        assert _cached_pool_count() == 1
        release_holder.set()
        return await waiter

    try:
        assert holder_ready.wait(10)
        pool = _run_in_fresh_loop(wait_then_release)
    finally:
        release_holder.set()
        holder.join(timeout=15)
    assert not holder.is_alive()
    assert pool.closed


def test_a_full_cap_times_out_instead_of_exceeding_it(monkeypatch):
    monkeypatch.setattr(pool_mod, "_MAX_POOLS", 1)
    monkeypatch.setattr(pool_mod, "_SLOT_WAIT_SECONDS", 0.2)
    holder_ready = threading.Event()
    release_holder = threading.Event()

    async def hold_a_slot():
        await _select_one()
        holder_ready.set()
        await asyncio.to_thread(release_holder.wait, 10)

    holder = threading.Thread(target=_run_in_fresh_loop, args=(hold_a_slot,))
    holder.start()
    try:
        assert holder_ready.wait(10)
        with pytest.raises(pool_mod.PoolTimeout):
            _run_in_fresh_loop(_select_one)
        assert _cached_pool_count() == 1
    finally:
        release_holder.set()
        holder.join(timeout=15)
    assert not holder.is_alive()


# The abandoned loop's pool tasks are destroyed pending; that noise is the scenario.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_a_loop_closed_without_shutdown_hooks_does_not_break_later_loops():
    """The other half of the cascade: cleanup on a later loop raised on the dead pool."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_select_one())
    finally:
        loop.close()  # no shutdown_asyncgens: the pool's hook never ran
    assert _backend_connections() >= 1

    async def later():
        await pool_mod.close_all_pools()
        return await _hold_connections(3)

    assert _run_in_fresh_loop(later).closed
    assert _cached_pool_count() == 0
    assert _settled_backend_connections() == 0
    gc.collect()  # surface the destroyed-task noise here, under this test's filter
