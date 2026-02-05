"""
Database connection pool manager for project databases.

This module provides thread-safe connection pooling for project database connections.
Each project gets its own connection pool to avoid creating new connections for every query.

Features:
- Thread-safe connection pooling via psycopg2.pool.ThreadedConnectionPool
- Lazy initialization (pools created on first use)
- Configurable max connections per project
- Automatic connection lifecycle management
- Context manager interface for safe connection handling
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from threading import Lock
from typing import TYPE_CHECKING, Generator

import psycopg2.pool
from django.conf import settings

if TYPE_CHECKING:
    from apps.projects.models import Project
    from psycopg2.extensions import connection as Psycopg2Connection

logger = logging.getLogger(__name__)


class ConnectionPoolManager:
    """
    Manages connection pools for multiple project databases.

    Each project gets its own ThreadedConnectionPool instance. Pools are created
    lazily when first accessed and maintained for the lifetime of the application.

    Thread-safe: Uses locks to prevent race conditions during pool creation.
    """

    def __init__(self, max_connections_per_project: int = 5):
        """
        Initialize the connection pool manager.

        Args:
            max_connections_per_project: Maximum number of connections per project pool.
                                        Default is 5. Can be overridden via
                                        MAX_CONNECTIONS_PER_PROJECT environment variable.
        """
        self._pools: dict[str, psycopg2.pool.ThreadedConnectionPool] = {}
        self._locks: dict[str, Lock] = {}
        self._global_lock = Lock()

        # Allow override from environment variable
        env_max_connections = getattr(settings, "MAX_CONNECTIONS_PER_PROJECT", None)
        self.max_connections_per_project = (
            env_max_connections if env_max_connections else max_connections_per_project
        )

        # Minimum connections to maintain in the pool (start small, grow as needed)
        self.min_connections_per_project = 1

        logger.info(
            "ConnectionPoolManager initialized with max_connections=%d per project",
            self.max_connections_per_project,
        )

    def _get_pool_key(self, project: Project) -> str:
        """
        Generate a unique key for a project's connection pool.

        Args:
            project: The Project instance

        Returns:
            A unique string key for the project's pool
        """
        return str(project.id)

    def _ensure_lock(self, pool_key: str) -> Lock:
        """
        Ensure a lock exists for the given pool key.

        Args:
            pool_key: The project pool key

        Returns:
            A threading.Lock instance for the pool
        """
        with self._global_lock:
            if pool_key not in self._locks:
                self._locks[pool_key] = Lock()
            return self._locks[pool_key]

    def _create_pool(self, project: Project, pool_key: str) -> psycopg2.pool.ThreadedConnectionPool:
        """
        Create a new connection pool for a project.

        Args:
            project: The Project instance
            pool_key: The unique pool key

        Returns:
            A ThreadedConnectionPool instance

        Raises:
            psycopg2.Error: If pool creation fails
        """
        conn_params = project.get_connection_params()

        # Create the pool with min and max connections
        pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=self.min_connections_per_project,
            maxconn=self.max_connections_per_project,
            **conn_params,
        )

        logger.info(
            "Created connection pool for project %s (id=%s) with %d-%d connections",
            project.slug,
            project.id,
            self.min_connections_per_project,
            self.max_connections_per_project,
        )

        return pool

    def get_pool(self, project: Project) -> psycopg2.pool.ThreadedConnectionPool:
        """
        Get or create a connection pool for a project.

        Pools are created lazily on first access. Thread-safe.

        Args:
            project: The Project instance

        Returns:
            A ThreadedConnectionPool instance for the project

        Raises:
            psycopg2.Error: If pool creation fails
        """
        pool_key = self._get_pool_key(project)

        # Always acquire lock first to prevent race conditions
        lock = self._ensure_lock(pool_key)
        with lock:
            # Check if pool exists after acquiring lock
            if pool_key in self._pools:
                return self._pools[pool_key]

            # Create the pool
            pool = self._create_pool(project, pool_key)
            self._pools[pool_key] = pool

            return pool

    @contextmanager
    def get_connection(
        self, project: Project
    ) -> Generator[Psycopg2Connection, None, None]:
        """
        Get a connection from the pool with automatic return.

        This is a context manager that ensures connections are always returned
        to the pool, even if an exception occurs.

        Args:
            project: The Project instance

        Yields:
            A psycopg2 connection instance

        Raises:
            psycopg2.Error: If connection acquisition fails

        Example:
            >>> with pool_manager.get_connection(project) as conn:
            ...     cursor = conn.cursor()
            ...     cursor.execute("SELECT * FROM users")
            ...     results = cursor.fetchall()
        """
        pool = self.get_pool(project)
        conn = None

        try:
            # Get connection from pool
            conn = pool.getconn()
            logger.debug(
                "Acquired connection for project %s (id=%s)",
                project.slug,
                project.id,
            )

            # Set connection to read-only mode for safety
            conn.set_session(readonly=True)

            yield conn

        except Exception:
            # On error, rollback and re-raise
            if conn:
                conn.rollback()
            raise

        finally:
            # Always return connection to pool
            if conn:
                # Reset connection state before returning to pool
                try:
                    conn.rollback()  # Rollback any uncommitted transactions
                except Exception as e:
                    logger.warning(
                        "Error rolling back connection for project %s: %s",
                        project.slug,
                        e,
                    )

                # Return to pool
                pool.putconn(conn)
                logger.debug(
                    "Released connection for project %s (id=%s)",
                    project.slug,
                    project.id,
                )

    def close_pool(self, project: Project) -> None:
        """
        Close and remove a project's connection pool.

        This closes all connections in the pool and removes the pool from the manager.
        Useful for cleanup or when project connection settings change.

        Args:
            project: The Project instance
        """
        pool_key = self._get_pool_key(project)

        if pool_key not in self._pools:
            logger.debug(
                "No pool to close for project %s (id=%s)",
                project.slug,
                project.id,
            )
            return

        lock = self._ensure_lock(pool_key)
        with lock:
            if pool_key in self._pools:
                pool = self._pools[pool_key]
                pool.closeall()
                del self._pools[pool_key]

                logger.info(
                    "Closed connection pool for project %s (id=%s)",
                    project.slug,
                    project.id,
                )

    def close_all(self) -> None:
        """
        Close all connection pools.

        This is typically called during application shutdown to cleanly
        close all database connections.
        """
        with self._global_lock:
            for pool_key in list(self._pools.keys()):
                pool = self._pools[pool_key]
                pool.closeall()
                logger.info("Closed connection pool: %s", pool_key)

            self._pools.clear()
            self._locks.clear()

        logger.info("All connection pools closed")


# Global singleton instance
_pool_manager: ConnectionPoolManager | None = None
_pool_manager_lock = Lock()


def get_pool_manager() -> ConnectionPoolManager:
    """
    Get the global ConnectionPoolManager singleton.

    Returns:
        The global ConnectionPoolManager instance
    """
    global _pool_manager

    if _pool_manager is not None:
        return _pool_manager

    with _pool_manager_lock:
        if _pool_manager is None:
            # Check for environment variable override
            max_connections = getattr(settings, "MAX_CONNECTIONS_PER_PROJECT", 5)
            _pool_manager = ConnectionPoolManager(max_connections_per_project=max_connections)

        return _pool_manager


__all__ = [
    "ConnectionPoolManager",
    "get_pool_manager",
]
