from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.test import override_settings
from psycopg.conninfo import conninfo_to_dict

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
        patch("apps.chat.checkpointer.CheckpointerPool", return_value=pool) as pool_cls,
        patch("apps.chat.checkpointer.AsyncPostgresSaver", return_value=saver),
    ):
        result = await checkpointer.ensure_checkpointer(force_new=True)

    assert result is saver
    pool_cls.assert_called_once_with(
        conninfo="postgresql://example/db",
        min_size=0,
        max_size=4,
        open=False,
        check=pool_cls.check_connection,
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


@pytest.mark.asyncio
async def test_init_failure_raises_and_caches_nothing(settings):
    """No MemorySaver fallback, even under DEBUG: the error surfaces and the next
    call retries instead of reusing an in-memory saver (#266 07#8)."""
    settings.DEBUG = True
    with (
        patch("apps.chat.checkpointer.get_database_url", return_value="postgresql://example/db"),
        patch("apps.chat.checkpointer.CheckpointerPool", side_effect=OSError("db down")),
        pytest.raises(OSError, match="db down"),
    ):
        await checkpointer.ensure_checkpointer()

    assert checkpointer._checkpointer is None


def _use_databases(monkeypatch, databases):
    # Patch the module's settings reference rather than DATABASES itself, which
    # Django warns against overriding under a live test database.
    monkeypatch.setattr(checkpointer, "settings", SimpleNamespace(DATABASES=databases))


def test_database_url_prefers_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db:6543/scout?sslmode=require")
    assert checkpointer.get_database_url() == "postgresql://u:p@db:6543/scout?sslmode=require"


def test_database_url_falls_back_to_settings_with_libpq_defaults(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _use_databases(
        monkeypatch,
        {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "scout",
                "HOST": "",
                "PORT": "",
                "USER": "",
                "PASSWORD": "",
            }
        },
    )
    assert conninfo_to_dict(checkpointer.get_database_url()) == {"dbname": "scout"}


def test_database_url_quotes_credentials(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _use_databases(
        monkeypatch,
        {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "scout",
                "HOST": "db",
                "PORT": 5432,
                "USER": "platform",
                "PASSWORD": "p@ss w/:?#",
            }
        },
    )
    assert conninfo_to_dict(checkpointer.get_database_url()) == {
        "dbname": "scout",
        "host": "db",
        "port": "5432",
        "user": "platform",
        "password": "p@ss w/:?#",
    }
