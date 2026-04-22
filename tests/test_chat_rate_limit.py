"""Tests for chat endpoint rate limiting."""

import time
from types import SimpleNamespace

import pytest
from django.core.cache import cache
from django.http import JsonResponse

from apps.chat.rate_limiting import (
    CHAT_RATE_LIMIT,
    CHAT_RATE_WINDOW,
    chat_rate_limit,
    check_and_record,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.asyncio
class TestCheckAndRecord:
    async def test_not_limited_initially(self):
        is_limited, info = await check_and_record(user_id=1)
        assert is_limited is False
        assert info["remaining"] == CHAT_RATE_LIMIT - 1  # recorded this request

    async def test_limited_after_max_requests(self):
        for _ in range(CHAT_RATE_LIMIT):
            await check_and_record(user_id=1)
        is_limited, info = await check_and_record(user_id=1)
        assert is_limited is True
        assert info["remaining"] == 0

    async def test_not_limited_below_threshold(self):
        for _ in range(CHAT_RATE_LIMIT - 1):
            await check_and_record(user_id=1)
        is_limited, info = await check_and_record(user_id=1)
        assert is_limited is False
        assert info["remaining"] == 0  # this was the last allowed request

    async def test_users_have_independent_limits(self):
        for _ in range(CHAT_RATE_LIMIT):
            await check_and_record(user_id=1)
        is_limited, _ = await check_and_record(user_id=2)
        assert is_limited is False

    async def test_remaining_decrements(self):
        _, info = await check_and_record(user_id=1)
        assert info["remaining"] == CHAT_RATE_LIMIT - 1

    async def test_window_expiry_resets_limit(self):
        """Timestamps outside the window are pruned."""
        cache_key = "chat_rl:1"
        old_ts = time.time() - CHAT_RATE_WINDOW - 1
        cache.set(cache_key, [old_ts] * CHAT_RATE_LIMIT, timeout=CHAT_RATE_WINDOW)

        is_limited, _info = await check_and_record(user_id=1)
        assert is_limited is False

    async def test_info_contains_reset_timestamp(self):
        _, info = await check_and_record(user_id=1)
        assert "reset" in info
        assert info["reset"] > time.time()

    async def test_does_not_record_when_limited(self):
        """When rate-limited, the request should NOT be recorded."""
        for _ in range(CHAT_RATE_LIMIT):
            await check_and_record(user_id=1)

        # This call should be rejected and not add another timestamp
        is_limited, _ = await check_and_record(user_id=1)
        assert is_limited is True

        # Count should still be exactly CHAT_RATE_LIMIT, not CHAT_RATE_LIMIT + 1
        timestamps = cache.get("chat_rl:1", [])
        assert len(timestamps) == CHAT_RATE_LIMIT

    @pytest.mark.django_db
    async def test_settings_override(self, settings):
        settings.CHAT_RATE_LIMIT = 2
        settings.CHAT_RATE_WINDOW = 10

        await check_and_record(user_id=1)
        await check_and_record(user_id=1)
        is_limited, info = await check_and_record(user_id=1)
        assert is_limited is True
        assert info["limit"] == 2


@pytest.mark.asyncio
class TestChatRateLimitDecorator:
    async def test_allows_request_under_limit(self):
        @chat_rate_limit
        async def view(request):
            return JsonResponse({"ok": True})

        request = SimpleNamespace(_authenticated_user=SimpleNamespace(pk=100))
        resp = await view(request)
        assert resp.status_code == 200

    async def test_blocks_request_over_limit(self):
        @chat_rate_limit
        async def view(request):
            return JsonResponse({"ok": True})

        request = SimpleNamespace(_authenticated_user=SimpleNamespace(pk=101))
        for _ in range(CHAT_RATE_LIMIT):
            await check_and_record(user_id=101)

        resp = await view(request)
        assert resp.status_code == 429
        assert "Retry-After" in resp

    async def test_records_request_on_success(self):
        @chat_rate_limit
        async def view(request):
            return JsonResponse({"ok": True})

        request = SimpleNamespace(_authenticated_user=SimpleNamespace(pk=102))
        await view(request)

        _, info = await check_and_record(user_id=102)
        assert info["remaining"] == CHAT_RATE_LIMIT - 2  # decorator + this check
