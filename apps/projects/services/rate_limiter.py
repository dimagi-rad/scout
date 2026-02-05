"""
Query rate limiting for the Scout data agent platform.

This module provides rate limiting for SQL queries to prevent abuse
and manage resource consumption. It supports:
- Per-user query rate limits
- Per-project daily query budgets
- Configurable limits via environment variables or project settings
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING

from django.conf import settings

if TYPE_CHECKING:
    from apps.projects.models import Project
    from apps.users.models import User

logger = logging.getLogger(__name__)


@dataclass
class RateLimitConfig:
    """Rate limit configuration."""

    # Per-user limits
    queries_per_minute: int = 10
    queries_per_hour: int = 100

    # Per-project limits
    queries_per_day: int = 1000


@dataclass
class QueryCount:
    """Tracks query counts for rate limiting."""

    count: int = 0
    window_start: float = field(default_factory=time.time)


class RateLimitExceeded(Exception):
    """Raised when a rate limit is exceeded."""

    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class QueryRateLimiter:
    """
    Rate limiter for SQL queries.

    Tracks query counts per user and per project to enforce rate limits.
    Uses in-memory storage for simplicity; in production with multiple
    workers, consider using Redis for distributed rate limiting.
    """

    def __init__(self, config: RateLimitConfig | None = None):
        """
        Initialize the rate limiter.

        Args:
            config: Rate limit configuration. If None, uses defaults.
        """
        self.config = config or RateLimitConfig()

        # Per-user tracking: {user_id: {window_type: QueryCount}}
        self._user_counts: dict[str, dict[str, QueryCount]] = defaultdict(dict)

        # Per-project tracking: {project_id: {window_type: QueryCount}}
        self._project_counts: dict[str, dict[str, QueryCount]] = defaultdict(dict)

        # Thread safety
        self._lock = Lock()

        logger.info(
            "QueryRateLimiter initialized: %d/min, %d/hour per user; %d/day per project",
            self.config.queries_per_minute,
            self.config.queries_per_hour,
            self.config.queries_per_day,
        )

    def check_rate_limit(
        self,
        user: User | None,
        project: Project,
    ) -> None:
        """
        Check if a query is allowed under current rate limits.

        Args:
            user: The user making the query (can be None for anonymous)
            project: The project being queried

        Raises:
            RateLimitExceeded: If any rate limit is exceeded
        """
        now = time.time()

        with self._lock:
            # Check user limits (if user is authenticated)
            if user:
                self._check_user_limits(str(user.id), now)

            # Check project limits
            self._check_project_limits(str(project.id), now)

    def record_query(
        self,
        user: User | None,
        project: Project,
    ) -> None:
        """
        Record a query for rate limiting tracking.

        Call this after successfully executing a query.

        Args:
            user: The user who made the query
            project: The project that was queried
        """
        now = time.time()

        with self._lock:
            if user:
                self._increment_user_counts(str(user.id), now)

            self._increment_project_counts(str(project.id), now)

    def get_remaining_quota(
        self,
        user: User | None,
        project: Project,
    ) -> dict:
        """
        Get the remaining query quota.

        Args:
            user: The user to check
            project: The project to check

        Returns:
            Dict with remaining queries for each limit type
        """
        now = time.time()

        with self._lock:
            result = {}

            if user:
                user_id = str(user.id)
                minute_count = self._get_window_count(
                    self._user_counts[user_id], "minute", now, 60
                )
                hour_count = self._get_window_count(
                    self._user_counts[user_id], "hour", now, 3600
                )
                result["user"] = {
                    "queries_per_minute": {
                        "used": minute_count,
                        "remaining": max(0, self.config.queries_per_minute - minute_count),
                        "limit": self.config.queries_per_minute,
                    },
                    "queries_per_hour": {
                        "used": hour_count,
                        "remaining": max(0, self.config.queries_per_hour - hour_count),
                        "limit": self.config.queries_per_hour,
                    },
                }

            project_id = str(project.id)
            day_count = self._get_window_count(
                self._project_counts[project_id], "day", now, 86400
            )
            result["project"] = {
                "queries_per_day": {
                    "used": day_count,
                    "remaining": max(0, self.config.queries_per_day - day_count),
                    "limit": self.config.queries_per_day,
                },
            }

            return result

    def _check_user_limits(self, user_id: str, now: float) -> None:
        """Check per-user rate limits."""
        counts = self._user_counts[user_id]

        # Check per-minute limit
        minute_count = self._get_window_count(counts, "minute", now, 60)
        if minute_count >= self.config.queries_per_minute:
            window_start = counts.get("minute", QueryCount()).window_start
            retry_after = int(60 - (now - window_start))
            raise RateLimitExceeded(
                f"Rate limit exceeded: {self.config.queries_per_minute} queries per minute",
                retry_after_seconds=max(1, retry_after),
            )

        # Check per-hour limit
        hour_count = self._get_window_count(counts, "hour", now, 3600)
        if hour_count >= self.config.queries_per_hour:
            window_start = counts.get("hour", QueryCount()).window_start
            retry_after = int(3600 - (now - window_start))
            raise RateLimitExceeded(
                f"Rate limit exceeded: {self.config.queries_per_hour} queries per hour",
                retry_after_seconds=max(1, retry_after),
            )

    def _check_project_limits(self, project_id: str, now: float) -> None:
        """Check per-project rate limits."""
        counts = self._project_counts[project_id]

        # Check per-day limit
        day_count = self._get_window_count(counts, "day", now, 86400)
        if day_count >= self.config.queries_per_day:
            window_start = counts.get("day", QueryCount()).window_start
            retry_after = int(86400 - (now - window_start))
            raise RateLimitExceeded(
                f"Project daily query limit exceeded: {self.config.queries_per_day} queries per day",
                retry_after_seconds=max(1, retry_after),
            )

    def _increment_user_counts(self, user_id: str, now: float) -> None:
        """Increment user query counts."""
        counts = self._user_counts[user_id]
        self._increment_window_count(counts, "minute", now, 60)
        self._increment_window_count(counts, "hour", now, 3600)

    def _increment_project_counts(self, project_id: str, now: float) -> None:
        """Increment project query counts."""
        counts = self._project_counts[project_id]
        self._increment_window_count(counts, "day", now, 86400)

    def _get_window_count(
        self,
        counts: dict[str, QueryCount],
        window_type: str,
        now: float,
        window_seconds: int,
    ) -> int:
        """Get count for a time window, resetting if window has expired."""
        if window_type not in counts:
            counts[window_type] = QueryCount(count=0, window_start=now)
            return 0

        qc = counts[window_type]
        if now - qc.window_start >= window_seconds:
            # Window expired, reset
            qc.count = 0
            qc.window_start = now
            return 0

        return qc.count

    def _increment_window_count(
        self,
        counts: dict[str, QueryCount],
        window_type: str,
        now: float,
        window_seconds: int,
    ) -> None:
        """Increment count for a time window."""
        if window_type not in counts:
            counts[window_type] = QueryCount(count=1, window_start=now)
            return

        qc = counts[window_type]
        if now - qc.window_start >= window_seconds:
            # Window expired, reset and start new count
            qc.count = 1
            qc.window_start = now
        else:
            qc.count += 1

    def reset_user(self, user_id: str) -> None:
        """Reset rate limit counts for a user (for testing)."""
        with self._lock:
            if user_id in self._user_counts:
                del self._user_counts[user_id]

    def reset_project(self, project_id: str) -> None:
        """Reset rate limit counts for a project (for testing)."""
        with self._lock:
            if project_id in self._project_counts:
                del self._project_counts[project_id]

    def reset_all(self) -> None:
        """Reset all rate limit counts (for testing)."""
        with self._lock:
            self._user_counts.clear()
            self._project_counts.clear()


# Global singleton instance
_rate_limiter: QueryRateLimiter | None = None
_rate_limiter_lock = Lock()


def get_rate_limiter() -> QueryRateLimiter:
    """
    Get the global QueryRateLimiter singleton.

    Returns:
        The global QueryRateLimiter instance
    """
    global _rate_limiter

    if _rate_limiter is not None:
        return _rate_limiter

    with _rate_limiter_lock:
        if _rate_limiter is None:
            # Load config from settings
            config = RateLimitConfig(
                queries_per_minute=getattr(settings, "RATE_LIMIT_QUERIES_PER_MINUTE", 10),
                queries_per_hour=getattr(settings, "RATE_LIMIT_QUERIES_PER_HOUR", 100),
                queries_per_day=getattr(settings, "RATE_LIMIT_QUERIES_PER_DAY", 1000),
            )
            _rate_limiter = QueryRateLimiter(config=config)

        return _rate_limiter


__all__ = [
    "QueryRateLimiter",
    "RateLimitConfig",
    "RateLimitExceeded",
    "get_rate_limiter",
]
