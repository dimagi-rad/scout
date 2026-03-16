"""Rate limiting helpers for authentication endpoints."""

from django.core.cache import cache

AUTH_MAX_ATTEMPTS = 5
AUTH_LOCKOUT_SECONDS = 300


def check_rate_limit(username: str) -> bool:
    """Return True if rate-limited (should block)."""
    return cache.get(f"auth_attempts:{username}", 0) >= AUTH_MAX_ATTEMPTS


def record_attempt(username: str, success: bool) -> None:
    key = f"auth_attempts:{username}"
    if success:
        cache.delete(key)
    else:
        cache.set(key, cache.get(key, 0) + 1, AUTH_LOCKOUT_SECONDS)
