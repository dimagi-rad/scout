"""Tests for rate limiting configuration."""

import pytest

from apps.users.rate_limiting import AUTH_MAX_ATTEMPTS, check_rate_limit, record_attempt


@pytest.mark.django_db
class TestRateLimiterBehavior:
    def test_not_limited_initially(self):
        assert check_rate_limit("fresh@example.com") is False

    def test_limited_after_max_attempts(self):
        email = "brute@example.com"
        for _ in range(AUTH_MAX_ATTEMPTS):
            record_attempt(email, success=False)
        assert check_rate_limit(email) is True

    def test_successful_login_resets_counter(self):
        email = "reset@example.com"
        for _ in range(AUTH_MAX_ATTEMPTS - 1):
            record_attempt(email, success=False)
        record_attempt(email, success=True)
        assert check_rate_limit(email) is False

    def test_not_limited_below_threshold(self):
        email = "partial@example.com"
        for _ in range(AUTH_MAX_ATTEMPTS - 1):
            record_attempt(email, success=False)
        assert check_rate_limit(email) is False


@pytest.mark.django_db
class TestRateLimitConfig:
    def test_drf_throttle_classes_configured(self, settings):
        """REST_FRAMEWORK should have DEFAULT_THROTTLE_CLASSES."""
        rf = settings.REST_FRAMEWORK
        assert "DEFAULT_THROTTLE_CLASSES" in rf
        assert len(rf["DEFAULT_THROTTLE_CLASSES"]) > 0

    def test_drf_throttle_rates_configured(self, settings):
        """REST_FRAMEWORK should have DEFAULT_THROTTLE_RATES."""
        rf = settings.REST_FRAMEWORK
        assert "DEFAULT_THROTTLE_RATES" in rf
        assert "anon" in rf["DEFAULT_THROTTLE_RATES"]
        assert "user" in rf["DEFAULT_THROTTLE_RATES"]
