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
        # Use get_or_set + incr for atomicity. get_or_set initializes to 0
        # with the lockout TTL if the key doesn't exist, then incr bumps it.
        cache.get_or_set(key, 0, AUTH_LOCKOUT_SECONDS)
        try:
            cache.incr(key)
        except ValueError:
            # Key expired between get_or_set and incr — set fresh
            cache.set(key, 1, AUTH_LOCKOUT_SECONDS)
