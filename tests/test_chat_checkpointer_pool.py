from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.test import override_settings

from apps.chat import checkpointer


@pytest.fixture(autouse=True)
def reset_checkpointer_singletons():
    checkpointer._checkpointer = None
    checkpointer._pool = None
    yield
    checkpointer._checkpointer = None
    checkpointer._pool = None


@pytest.mark.asyncio
async def test_ensure_checkpointer_uses_configured_pool_limits():
    pool = MagicMock()
    pool.open = AsyncMock()
    saver = MagicMock()
    saver.setup = AsyncMock()

    with (
        override_settings(
            LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE=0,
            LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE=4,
            LANGGRAPH_CHECKPOINT_POOL_OPEN_TIMEOUT_S=3,
        ),
        patch("apps.chat.checkpointer.get_database_url", return_value="postgresql://example/db"),
        patch("apps.chat.checkpointer.AsyncConnectionPool", return_value=pool) as pool_cls,
        patch("apps.chat.checkpointer.AsyncPostgresSaver", return_value=saver),
    ):
        result = await checkpointer.ensure_checkpointer(force_new=True)

    assert result is saver
    pool_cls.assert_called_once_with(
        conninfo="postgresql://example/db",
        min_size=0,
        max_size=4,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
        },
    )
    pool.open.assert_awaited_once_with(wait=True, timeout=3)
    saver.setup.assert_awaited_once()


def test_pool_config_rejects_min_size_above_max_size():
    with override_settings(
        LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE=5,
        LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE=4,
        LANGGRAPH_CHECKPOINT_POOL_OPEN_TIMEOUT_S=10,
    ):
        with pytest.raises(ValueError, match="MIN_SIZE must be <= .*MAX_SIZE"):
            checkpointer._get_pool_config()
