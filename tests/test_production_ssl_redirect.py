"""SECURE_SSL_REDIRECT parses as a bool, so "False" (config/deploy.yml) turns it off."""

import sys

import pytest

from tests.production_settings import PRODUCTION_SETTINGS, load_production_settings


@pytest.fixture(autouse=True)
def _mcp_secret(monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-secret")
    yield
    sys.modules.pop(PRODUCTION_SETTINGS, None)


def test_ssl_redirect_defaults_on(monkeypatch):
    monkeypatch.delenv("SECURE_SSL_REDIRECT", raising=False)
    assert load_production_settings().SECURE_SSL_REDIRECT is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [("False", False), ("false", False), ("0", False), ("True", True), ("1", True)],
)
def test_ssl_redirect_env_is_parsed_as_bool(monkeypatch, value, expected):
    monkeypatch.setenv("SECURE_SSL_REDIRECT", value)
    assert load_production_settings().SECURE_SSL_REDIRECT is expected
