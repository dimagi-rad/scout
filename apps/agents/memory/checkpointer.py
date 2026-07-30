"""
PostgreSQL checkpointer for LangGraph conversation persistence.

Async-compatible Postgres checkpoint storage for LangGraph agents, falling back
to MemorySaver in tests or when the Postgres connection fails.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from langgraph.checkpoint.memory import MemorySaver

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

logger = logging.getLogger(__name__)


def get_database_url() -> str:
    """Resolve the Postgres URL: DATABASE_URL, then DB_* env vars, then Django
    DATABASES['default']. Raises ValueError if none yield a valid config.
    """
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        logger.debug("Using DATABASE_URL environment variable")
        return database_url

    db_host = os.environ.get("DB_HOST")
    db_name = os.environ.get("DB_NAME")
    db_user = os.environ.get("DB_USER")
    db_password = os.environ.get("DB_PASSWORD")
    db_port = os.environ.get("DB_PORT", "5432")

    if all([db_host, db_name, db_user]):
        password_part = f":{db_password}" if db_password else ""
        url = f"postgresql://{db_user}{password_part}@{db_host}:{db_port}/{db_name}"
        logger.debug("Using individual DB_* environment variables")
        return url

    try:
        from django.conf import settings

        db_config = settings.DATABASES.get("default", {})
        engine = db_config.get("ENGINE", "")

        if "postgresql" not in engine.lower() and "postgres" not in engine.lower():
            raise ValueError(f"Django default database is not PostgreSQL: {engine}")

        host = db_config.get("HOST", "localhost")
        port = db_config.get("PORT", 5432)
        name = db_config.get("NAME")
        user = db_config.get("USER")
        password = db_config.get("PASSWORD", "")

        if not all([host, name, user]):
            raise ValueError("Incomplete Django database configuration")

        password_part = f":{password}" if password else ""
        url = f"postgresql://{user}{password_part}@{host}:{port}/{name}"
        logger.debug("Using Django DATABASES settings")
        return url

    except Exception as e:
        raise ValueError(f"Unable to construct database URL: {e}") from e


@asynccontextmanager
async def get_postgres_checkpointer() -> AsyncGenerator[BaseCheckpointSaver, None]:
    """Async context manager yielding an AsyncPostgresSaver (running setup() to
    create tables) connected to the platform DB. Falls back to MemorySaver in
    test mode (TESTING env var), when the connection fails, or when
    langgraph-checkpoint-postgres is unavailable.
    """
    if os.environ.get("TESTING", "").lower() in ("1", "true", "yes"):
        logger.info("Test mode detected, using MemorySaver")
        yield MemorySaver()
        return

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        database_url = get_database_url()
        logger.info("Connecting to PostgreSQL for checkpointing")

        async with AsyncPostgresSaver.from_conn_string(database_url) as checkpointer:
            await checkpointer.setup()
            logger.info("PostgreSQL checkpointer initialized successfully")
            yield checkpointer

    except ImportError as e:
        logger.warning(
            "langgraph-checkpoint-postgres not available, falling back to MemorySaver. "
            "Conversations will NOT be persisted across sessions. "
            "Install langgraph-checkpoint-postgres for persistent storage. Error: %s",
            e,
        )
        yield MemorySaver()

    except Exception as e:
        logger.warning(
            "Failed to connect to PostgreSQL for checkpointing, falling back to MemorySaver. "
            "Conversations will NOT be persisted across sessions. "
            "Check your database configuration. Error: %s",
            e,
        )
        yield MemorySaver()


def get_sync_checkpointer() -> BaseCheckpointSaver:
    """Return a MemorySaver for sync contexts (management commands, tests).
    Production async usage should use get_postgres_checkpointer().
    """
    logger.debug("Creating synchronous MemorySaver checkpointer")
    return MemorySaver()


__all__ = [
    "get_database_url",
    "get_postgres_checkpointer",
    "get_sync_checkpointer",
]
