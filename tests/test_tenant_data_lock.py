"""Tenant advisory-lock namespace: order, dedup, reentrancy and failure modes (D1)."""

import asyncio
import threading
import uuid
from unittest.mock import patch

import psycopg
import pytest

from apps.workspaces.services import data_operation
from apps.workspaces.services.data_operation import (
    _TENANT_LOCK_NAMESPACE,
    DataLockTimeout,
    LockOrderError,
    tenant_data_lock,
    tenant_lock_key,
    tenant_lock_keys,
    workspace_data_lock,
)
from tests.tenant_lock_probe import try_tenant_data_lock

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


def _held_tenant_locks(conn):
    rows = conn.execute(
        "SELECT objid FROM pg_locks WHERE locktype = 'advisory' AND classid = %s AND granted",
        (_TENANT_LOCK_NAMESPACE & 0xFFFFFFFF,),
    ).fetchall()
    return sorted(row[0] for row in rows)


@pytest.fixture
def probe():
    from django.db import connections

    params = connections["default"].get_connection_params()
    params.pop("cursor_factory", None)
    conn = psycopg.connect(**params, autocommit=True)
    yield conn
    conn.close()


async def test_keys_are_sorted_and_deduplicated_physical_keys():
    a, b = uuid.uuid4(), uuid.uuid4()
    keys = tenant_lock_keys([b, a, str(a), b])
    assert keys == tuple(sorted({tenant_lock_key(a), tenant_lock_key(b)}))


async def test_reversed_order_requests_do_not_deadlock():
    a, b = uuid.uuid4(), uuid.uuid4()
    first_in = asyncio.Event()
    second_in = asyncio.Event()

    async def holder(order, entered):
        async with tenant_data_lock(order):
            entered.set()
            await asyncio.sleep(0.05)

    await asyncio.wait_for(
        asyncio.gather(holder([a, b], first_in), holder([b, a], second_in)), timeout=5
    )
    assert first_in.is_set() and second_in.is_set()


async def test_forced_key_collision_acquires_one_physical_lock(probe):
    a, b = uuid.uuid4(), uuid.uuid4()
    shared = tenant_lock_key(a)
    with patch.object(data_operation, "tenant_lock_key", return_value=shared):
        assert tenant_lock_keys([a, b]) == (shared,)
        async with tenant_data_lock([b, a]):
            assert _held_tenant_locks(probe).count(shared & 0xFFFFFFFF) == 1
        # Reverse order with colliding keys cannot deadlock either.
        async with asyncio.timeout(5), tenant_data_lock([a, b]):
            assert _held_tenant_locks(probe).count(shared & 0xFFFFFFFF) == 1


async def test_same_task_reuses_held_subset_but_rejects_expansion():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with tenant_data_lock([a, b]):
        async with tenant_data_lock([b]):
            pass
        async with tenant_data_lock([a, b]):
            pass
        with pytest.raises(LockOrderError):
            async with tenant_data_lock([c]):
                pass
        with pytest.raises(LockOrderError):
            async with tenant_data_lock([a, c]):
                pass


async def test_child_task_needs_its_own_session_and_waits():
    a = uuid.uuid4()
    child_acquired = asyncio.Event()

    async def child():
        async with tenant_data_lock([a]):
            child_acquired.set()

    async with tenant_data_lock([a]):
        task = asyncio.create_task(child())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(child_acquired.wait(), timeout=0.1)
    await asyncio.wait_for(task, timeout=5)
    assert child_acquired.is_set()


async def test_workspace_lock_ownership_is_not_tenant_lock_ownership():
    key = uuid.uuid4()
    entered = asyncio.Event()

    async def other():
        async with tenant_data_lock([key]):
            entered.set()

    async with workspace_data_lock(key):
        task = asyncio.create_task(other())
        await asyncio.wait_for(entered.wait(), timeout=5)
        await task


async def test_cancelled_holder_releases_tenant_locks():
    a = uuid.uuid4()
    entered = asyncio.Event()

    async def holder():
        async with tenant_data_lock([a]):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(holder())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(5):
        async with tenant_data_lock([a]):
            pass


async def test_lock_timeout_fails_visibly():
    a = uuid.uuid4()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with tenant_data_lock([a]):
            entered.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await entered.wait()
    try:
        with patch.object(data_operation, "_LOCK_TIMEOUT", "200ms"), pytest.raises(DataLockTimeout):
            async with tenant_data_lock([a]):
                pass
    finally:
        release.set()
        await task


async def test_drained_thread_reuses_only_its_originating_task_locks():
    workspace, tenant = uuid.uuid4(), uuid.uuid4()

    def nested():
        with data_operation.sync_workspace_data_lock(workspace):
            with data_operation.sync_tenant_data_lock([tenant]):
                return True

    with patch.object(data_operation, "_LOCK_TIMEOUT", "200ms"):
        async with workspace_data_lock(workspace), tenant_data_lock([tenant]):
            assert await data_operation.run_data_thread(nested)
            with pytest.raises(LockOrderError):
                await data_operation.run_data_thread(
                    lambda: _expand_sync_tenants(tenant, uuid.uuid4())
                )


def _expand_sync_tenants(first, second):
    with data_operation.sync_tenant_data_lock([first, second]):
        pass


async def test_child_drained_thread_does_not_borrow_parent_tenant_lock():
    tenant = uuid.uuid4()
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def child_thread():
        with data_operation.sync_tenant_data_lock([tenant]):
            loop.call_soon_threadsafe(entered.set)

    async with tenant_data_lock([tenant]):
        child = asyncio.create_task(data_operation.run_data_thread(child_thread))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(entered.wait(), 0.1)
    await asyncio.wait_for(child, 5)
    assert entered.is_set()


async def test_cancelled_drained_thread_keeps_parent_tenant_ownership_until_stopped():
    tenant = uuid.uuid4()
    entered = asyncio.Event()
    stopped = asyncio.Event()
    gate = threading.Event()
    loop = asyncio.get_running_loop()

    def work():
        with data_operation.sync_tenant_data_lock([tenant]):
            loop.call_soon_threadsafe(entered.set)
            gate.wait(5)
            loop.call_soon_threadsafe(stopped.set)

    async def owner():
        async with tenant_data_lock([tenant]):
            await data_operation.run_data_thread(work)

    task = asyncio.create_task(owner())
    await entered.wait()
    try:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not stopped.is_set()
        with try_tenant_data_lock(tenant) as held:
            assert not held
    finally:
        gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    with try_tenant_data_lock(tenant) as held:
        assert held
