"""Production settings refuse to start without MCP_SHARED_SECRET (#51).

The MCP server rejects every request when its secret is empty, so a production
process booting without one is misconfigured; fail at import, not at the first
agent tool call.
"""

import pytest
from django.core.exceptions import ImproperlyConfigured

from tests.production_settings import load_production_settings


@pytest.mark.parametrize("value", [None, "", "   "])
def test_production_settings_raise_without_secret(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("MCP_SHARED_SECRET", raising=False)
    else:
        monkeypatch.setenv("MCP_SHARED_SECRET", value)
    with pytest.raises(ImproperlyConfigured, match="MCP_SHARED_SECRET"):
        load_production_settings()


def test_production_settings_load_with_secret(production_settings):
    assert production_settings.MCP_SHARED_SECRET == "test-secret"


@pytest.mark.parametrize("value", ["test-secret\n", " test-secret \r\n"])
def test_production_settings_strip_surrounding_whitespace(monkeypatch, value):
    """E2: secret stores often append a newline; the stripped value is what's used."""
    monkeypatch.setenv("MCP_SHARED_SECRET", value)
    assert load_production_settings().MCP_SHARED_SECRET == "test-secret"
