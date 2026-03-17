"""Per-user rate limiting for async Django views.

DRF throttle classes don't apply to raw async views, so this module
provides a sliding-window counter using Django's async cache API,
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


async def check_and_record(user_id) -> tuple[bool, dict]:
    """Atomically check the rate limit and record the request if allowed.

    Performs a single cache read/write cycle to avoid TOCTOU races
    where concurrent requests could all pass the check before any
    records a timestamp.

    Returns (is_limited, info) where *info* contains ``limit``,
    ``remaining``, and ``reset`` (epoch timestamp).
    """
    limit, window = _get_settings()
    now = time.time()
    cache_key = f"chat_rl:{user_id}"
    cutoff = now - window

    timestamps: list[float] = await cache.aget(cache_key, [])
    timestamps = [t for t in timestamps if t > cutoff]

    reset = int(now + window)

    if len(timestamps) >= limit:
        return True, {"limit": limit, "remaining": 0, "reset": reset}

    timestamps.append(now)
    await cache.aset(cache_key, timestamps, timeout=window)
    remaining = max(0, limit - len(timestamps))
    return False, {"limit": limit, "remaining": remaining, "reset": reset}


def chat_rate_limit(view_func):
    """Decorator that enforces per-user chat rate limiting.

    Must be applied *after* ``@async_login_required`` so that
    ``request._authenticated_user`` is available.
    """

    @functools.wraps(view_func)
    async def wrapper(request, *args, **kwargs):
        user = request._authenticated_user
        is_limited, rl_info = await check_and_record(user.pk)
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

        return await view_func(request, *args, **kwargs)

    return wrapper
