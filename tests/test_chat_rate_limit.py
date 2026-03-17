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
    check_chat_rate_limit,
    record_chat_request,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


class TestChatRateLimiting:
    def test_not_limited_initially(self):
        is_limited, info = check_chat_rate_limit(user_id=1)
        assert is_limited is False
        assert info["remaining"] == CHAT_RATE_LIMIT

    def test_limited_after_max_requests(self):
        for _ in range(CHAT_RATE_LIMIT):
            record_chat_request(user_id=1)
        is_limited, info = check_chat_rate_limit(user_id=1)
        assert is_limited is True
        assert info["remaining"] == 0

    def test_not_limited_below_threshold(self):
        for _ in range(CHAT_RATE_LIMIT - 1):
            record_chat_request(user_id=1)
        is_limited, info = check_chat_rate_limit(user_id=1)
        assert is_limited is False
        assert info["remaining"] == 1

    def test_users_have_independent_limits(self):
        for _ in range(CHAT_RATE_LIMIT):
            record_chat_request(user_id=1)
        is_limited, _ = check_chat_rate_limit(user_id=2)
        assert is_limited is False

    def test_remaining_decrements(self):
        record_chat_request(user_id=1)
        _, info = check_chat_rate_limit(user_id=1)
        assert info["remaining"] == CHAT_RATE_LIMIT - 1

    def test_window_expiry_resets_limit(self):
        """Timestamps outside the window are pruned."""
        cache_key = "chat_rl:1"
        old_ts = time.time() - CHAT_RATE_WINDOW - 1
        cache.set(cache_key, [old_ts] * CHAT_RATE_LIMIT, timeout=CHAT_RATE_WINDOW)

        is_limited, info = check_chat_rate_limit(user_id=1)
        assert is_limited is False
        assert info["remaining"] == CHAT_RATE_LIMIT

    def test_info_contains_reset_timestamp(self):
        _, info = check_chat_rate_limit(user_id=1)
        assert "reset" in info
        assert info["reset"] > time.time()

    @pytest.mark.django_db
    def test_settings_override(self, settings):
        settings.CHAT_RATE_LIMIT = 2
        settings.CHAT_RATE_WINDOW = 10

        record_chat_request(user_id=1)
        record_chat_request(user_id=1)
        is_limited, info = check_chat_rate_limit(user_id=1)
        assert is_limited is True
        assert info["limit"] == 2


class TestChatRateLimitDecorator:
    @pytest.mark.asyncio
    async def test_allows_request_under_limit(self):
        @chat_rate_limit
        async def view(request):
            return JsonResponse({"ok": True})

        request = SimpleNamespace(_authenticated_user=SimpleNamespace(pk=100))
        resp = await view(request)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_blocks_request_over_limit(self):
        @chat_rate_limit
        async def view(request):
            return JsonResponse({"ok": True})

        request = SimpleNamespace(_authenticated_user=SimpleNamespace(pk=101))
        for _ in range(CHAT_RATE_LIMIT):
            record_chat_request(user_id=101)

        resp = await view(request)
        assert resp.status_code == 429
        assert "Retry-After" in resp

    @pytest.mark.asyncio
    async def test_records_request_on_success(self):
        @chat_rate_limit
        async def view(request):
            return JsonResponse({"ok": True})

        request = SimpleNamespace(_authenticated_user=SimpleNamespace(pk=102))
        await view(request)

        _, info = check_chat_rate_limit(user_id=102)
        assert info["remaining"] == CHAT_RATE_LIMIT - 1
