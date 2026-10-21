"""Per-user rate limiting for async Django views.

DRF throttle classes don't apply to raw async views, so this module
provides a sliding-window counter using Django's async cache API,
exposed as a decorator.
"""

import functools
import time

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse

# Defaults — override via Django settings if needed.
CHAT_RATE_LIMIT = 20  # requests per window
CHAT_RATE_WINDOW = 60  # seconds
# Separate from the chat budget: one message can carry ten attachments.
ATTACHMENT_UPLOAD_RATE_LIMIT = 60


def _get_settings():
    """Read overrides from Django settings, falling back to module defaults."""

    limit = getattr(settings, "CHAT_RATE_LIMIT", CHAT_RATE_LIMIT)
    window = getattr(settings, "CHAT_RATE_WINDOW", CHAT_RATE_WINDOW)
    return limit, window


async def check_and_record(
    user_id, *, scope: str = "chat", limit: int | None = None
) -> tuple[bool, dict]:
    """Atomically check the rate limit and record the request if allowed.

    Performs a single cache read/write cycle to avoid TOCTOU races
    where concurrent requests could all pass the check before any
    records a timestamp.

    Returns (is_limited, info) where *info* contains ``limit``,
    ``remaining``, and ``reset`` (epoch timestamp).
    """
    default_limit, window = _get_settings()
    limit = default_limit if limit is None else limit
    now = time.time()
    cache_key = f"{scope}_rl:{user_id}"
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
    return _rate_limited(
        view_func,
        scope="chat",
        limit=lambda: _get_settings()[0],
        message="Rate limit exceeded. Please wait before sending another message.",
    )


def attachment_upload_rate_limit(view_func):
    """Per-user limit on attachment uploads, counted apart from chat messages."""
    return _rate_limited(
        view_func,
        scope="attachment_upload",
        limit=lambda: getattr(
            settings, "ATTACHMENT_UPLOAD_RATE_LIMIT", ATTACHMENT_UPLOAD_RATE_LIMIT
        ),
        message="Too many uploads. Please wait a moment before attaching more files.",
        reason="attachment_rate_limited",
    )


def _rate_limited(view_func, *, scope: str, limit, message: str, reason=None):
    """``limit`` is a callable read per request, so a settings override applies at once."""

    @functools.wraps(view_func)
    async def wrapper(request, *args, **kwargs):
        user = request._authenticated_user
        is_limited, rl_info = await check_and_record(user.pk, scope=scope, limit=limit())
        if is_limited:
            body = {"error": message}
            if reason:
                body["reason"] = reason
            resp = JsonResponse(body, status=429)
            resp["Retry-After"] = str(rl_info["reset"] - int(time.time()))
            resp["X-RateLimit-Limit"] = str(rl_info["limit"])
            resp["X-RateLimit-Remaining"] = "0"
            resp["X-RateLimit-Reset"] = str(rl_info["reset"])
            return resp

        return await view_func(request, *args, **kwargs)

    return wrapper
