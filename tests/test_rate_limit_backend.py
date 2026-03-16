"""Tests for rate limiting configuration."""

import pytest


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
