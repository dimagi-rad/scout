"""Cache backend selection (arch #254, finding 06#2).

Chat rate limiting and DRF throttles store counters in the Django default
cache, which was a per-process ``LocMemCache`` — ineffective across the 4
uvicorn workers, so the 20-req/60s budget was effectively multiplied by worker
count and reset on every deploy. ElastiCache Redis is provisioned but unused.

The fix backs the default cache with the shared Redis cache **when REDIS_URL is
set**, and keeps ``LocMemCache`` as the fallback for dev/test/CI where Redis
isn't reachable. These tests exercise the selection logic directly (no live
Redis needed).
"""

import inspect

from apps.chat import rate_limiting
from config.settings.base import _build_caches


def test_locmem_fallback_when_no_redis_url():
    caches = _build_caches("")
    assert caches["default"]["BACKEND"] == "django.core.cache.backends.locmem.LocMemCache"


def test_redis_backend_when_redis_url_set():
    caches = _build_caches("redis://cache.internal:6379/0")
    assert caches["default"]["BACKEND"] == "django.core.cache.backends.redis.RedisCache"
    assert caches["default"]["LOCATION"] == "redis://cache.internal:6379/0"


def test_rate_limiter_uses_default_cache():
    """The chat rate limiter must read/write the Django *default* cache (so a
    Redis-backed default makes it shared across workers), not a private dict.
    """
    src = inspect.getsource(rate_limiting)
    # It uses the shared django cache API, not a module-level dict counter.
    assert "from django.core.cache import cache" in src
    assert "cache.aget" in src
    assert "cache.aset" in src
