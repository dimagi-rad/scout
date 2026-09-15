import asyncio
import threading

import pytest

from apps.workspaces.services.data_operation import (
    run_data_thread,
    serialized_workspace_data,
    workspace_data_lock,
)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_data_operation_lock_is_shared_and_reentrant(workspace):
    waiting = asyncio.Event()
    acquired = asyncio.Event()

    async def competing_operation():
        waiting.set()
        async with workspace_data_lock(workspace.id):
            acquired.set()

    async with workspace_data_lock(workspace.id):
        async with workspace_data_lock(workspace.id):
            competitor = asyncio.create_task(competing_operation())
            await waiting.wait()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(acquired.wait(), timeout=0.05)
        assert not acquired.is_set()
    await asyncio.wait_for(competitor, timeout=2)
    assert acquired.is_set()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancelled_data_operation_releases_lock(workspace):
    entered = asyncio.Event()

    async def operation():
        async with workspace_data_lock(workspace.id):
            entered.set()
            await asyncio.Event().wait()

    owner = asyncio.create_task(operation())
    await entered.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    async with asyncio.timeout(2):
        async with workspace_data_lock(workspace.id):
            pass


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("publisher_fails", [False, True])
async def test_cancelled_publication_holds_lock_until_thread_stops(workspace, publisher_fails):
    entered = asyncio.Event()
    acquired = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def publish():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5)
        if publisher_fails:
            raise RuntimeError("Publisher failed during cancellation")

    @serialized_workspace_data
    async def operation(workspace_id):
        await run_data_thread(publish)

    async def contend():
        async with workspace_data_lock(workspace.id):
            acquired.set()

    owner = asyncio.create_task(operation(workspace.id))
    contender = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        owner.cancel()
        contender = asyncio.create_task(contend())
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(acquired.wait(), timeout=0.05)
            assert not owner.done()
            owner.cancel()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, timeout=2)
        if contender is not None:
            await asyncio.wait_for(contender, timeout=2)
    assert acquired.is_set()
