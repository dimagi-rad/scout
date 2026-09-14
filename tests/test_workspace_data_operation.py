import asyncio

import pytest

from apps.workspaces.services.data_operation import workspace_data_lock


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
