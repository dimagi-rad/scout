"""
PostgreSQL checkpointer for LangGraph conversation persistence.

This module provides async-compatible PostgreSQL-based checkpoint storage
for LangGraph agents, enabling conversation state persistence across sessions.

The checkpointer uses the platform's Django database configuration and
falls back to MemorySaver for testing or when PostgreSQL connection fails.
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
    """
    Get the PostgreSQL connection URL from environment or Django settings.

    Attempts to get the connection URL in this order:
    1. DATABASE_URL environment variable
    2. Individual DB_* environment variables
    3. Django DATABASES['default'] settings

    Returns:
        PostgreSQL connection URL string.

    Raises:
        ValueError: If no valid database configuration is found.
    """
    # Try DATABASE_URL first
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        logger.debug("Using DATABASE_URL environment variable")
        return database_url

    # Try individual environment variables
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

    # Fall back to Django settings
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
    """
    Create an async PostgreSQL checkpointer for LangGraph.

    This async context manager creates an AsyncPostgresSaver instance
    connected to the platform database. It handles setup (creating tables
    if needed) and proper connection cleanup.

    Falls back to MemorySaver if:
    - PostgreSQL connection fails
    - Running in test mode (TESTING=1 environment variable)
    - langgraph-checkpoint-postgres is not available

    Yields:
        BaseCheckpointSaver: Either an AsyncPostgresSaver or MemorySaver instance.

    Example:
        async with get_postgres_checkpointer() as checkpointer:
            graph = build_agent_graph(project, checkpointer=checkpointer)
            result = await graph.ainvoke(...)
    """
    # Check if we're in test mode
    if os.environ.get("TESTING", "").lower() in ("1", "true", "yes"):
        logger.info("Test mode detected, using MemorySaver")
        yield MemorySaver()
        return

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        database_url = get_database_url()
        logger.info("Connecting to PostgreSQL for checkpointing")

        async with AsyncPostgresSaver.from_conn_string(database_url) as checkpointer:
            # Setup creates the checkpoint tables if they don't exist
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
    """
    Get a synchronous checkpointer for non-async contexts.

    For synchronous usage (e.g., management commands, testing), this returns
    a MemorySaver. For production async usage, use get_postgres_checkpointer().

    Returns:
        MemorySaver instance for synchronous operations.
    """
    logger.debug("Creating synchronous MemorySaver checkpointer")
    return MemorySaver()


__all__ = [
    "get_database_url",
    "get_postgres_checkpointer",
    "get_sync_checkpointer",
]
