"""Per-user rate limiting for async Django views.

DRF throttle classes don't apply to raw async views, so this module
provides a sliding-window counter using Django's cache framework,
exposed as a decorator.
"""

import functools
import time

from django.core.cache import cache
from django.http import JsonResponse

# Defaults — override via Django settings if needed.
CHAT_RATE_LIMIT = 20  # max requests per window
CHAT_RATE_WINDOW = 60  # window in seconds


def _get_settings():
    """Read overrides from Django settings, falling back to module defaults."""
    from django.conf import settings

    limit = getattr(settings, "CHAT_RATE_LIMIT", CHAT_RATE_LIMIT)
    window = getattr(settings, "CHAT_RATE_WINDOW", CHAT_RATE_WINDOW)
    return limit, window


def check_chat_rate_limit(user_id) -> tuple[bool, dict]:
    """Check whether *user_id* has exceeded the chat rate limit.

    Returns (is_limited, info) where *info* contains ``limit``,
    ``remaining``, and ``reset`` (epoch timestamp).
    """
    limit, window = _get_settings()
    now = time.time()
    cache_key = f"chat_rl:{user_id}"

    # Stored value: list of request timestamps within the current window.
    timestamps: list[float] = cache.get(cache_key, [])

    # Prune entries outside the window.
    cutoff = now - window
    timestamps = [t for t in timestamps if t > cutoff]

    reset = int(now + window)
    remaining = max(0, limit - len(timestamps))

    if len(timestamps) >= limit:
        return True, {"limit": limit, "remaining": 0, "reset": reset}

    return False, {"limit": limit, "remaining": remaining, "reset": reset}


def record_chat_request(user_id) -> None:
    """Record a chat request for the sliding window counter."""
    _, window = _get_settings()
    now = time.time()
    cache_key = f"chat_rl:{user_id}"

    timestamps: list[float] = cache.get(cache_key, [])
    cutoff = now - window
    timestamps = [t for t in timestamps if t > cutoff]
    timestamps.append(now)
    cache.set(cache_key, timestamps, timeout=window)


def chat_rate_limit(view_func):
    """Decorator that enforces per-user chat rate limiting.

    Must be applied *after* ``@async_login_required`` so that
    ``request._authenticated_user`` is available.
    """

    @functools.wraps(view_func)
    async def wrapper(request, *args, **kwargs):
        user = request._authenticated_user
        is_limited, rl_info = check_chat_rate_limit(user.pk)
        if is_limited:
            resp = JsonResponse(
                {"error": "Rate limit exceeded. Please wait before sending another message."},
                status=429,
            )
            resp["Retry-After"] = str(rl_info["reset"] - int(time.time()))
            resp["X-RateLimit-Limit"] = str(rl_info["limit"])
            resp["X-RateLimit-Remaining"] = "0"
            resp["X-RateLimit-Reset"] = str(rl_info["reset"])
            return resp

        record_chat_request(user.pk)
        return await view_func(request, *args, **kwargs)

    return wrapper
