"""Truthful-failure test for login-time tenant resolution (arch #256, 07#6).

Post-OAuth tenant resolution for all three providers was wrapped in
``except Exception: logger.warning(...)``. A provider-API blip at login then
yielded a logged-in user with NO TenantMembership rows and an empty data-sources
page — indistinguishable from "account has no opportunities" — and the WARNING
was suppressed by Sentry's ERROR event-level default, so nobody was told.

The handler must still not break login (a resolution failure can't 500 the OAuth
callback), but it must surface the failure at ERROR level so Sentry pages and an
operator can tell "resolution failed" from "no opportunities."
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apps.users import signals

SIGNALS_LOGGER = "apps.users.signals"


def _sociallogin(provider: str, token: str = "tok"):
    return SimpleNamespace(
        account=SimpleNamespace(provider=provider),
        token=SimpleNamespace(token=token),
        user=SimpleNamespace(pk=1, email="u@b.c"),
    )


@pytest.mark.parametrize(
    ("provider", "resolver_name"),
    [
        ("commcare_connect", "resolve_connect_opportunities"),
        ("ocs", "resolve_ocs_chatbots"),
        ("commcare", "resolve_commcare_domains"),
    ],
)
def test_resolution_failure_logs_at_error_not_warning(provider, resolver_name, caplog):
    """A provider-API failure must be logged at ERROR (Sentry pages) and must not
    propagate out of the handler (login still succeeds)."""
    sl = _sociallogin(provider)

    failing = AsyncMock(side_effect=RuntimeError("provider 503"))
    with patch.object(signals, resolver_name, failing):
        with caplog.at_level(logging.WARNING, logger=SIGNALS_LOGGER):
            # Must not raise — login must not break.
            signals.resolve_tenant_on_social_login(request=None, sociallogin=sl)

    records = [r for r in caplog.records if r.name == SIGNALS_LOGGER]
    levels = {r.levelno for r in records}
    assert logging.ERROR in levels, "resolution failure must page (ERROR), not be a quiet WARNING"


def test_missing_token_still_warns_and_returns():
    """No token at all is a benign 'nothing to resolve' — stays a WARNING and the
    handler returns without attempting resolution."""
    sl = SimpleNamespace(
        account=SimpleNamespace(provider="ocs"),
        token=SimpleNamespace(token=""),
        user=SimpleNamespace(pk=1, email="u@b.c"),
    )
    with patch.object(signals, "resolve_ocs_chatbots", AsyncMock()) as resolver:
        signals.resolve_tenant_on_social_login(request=None, sociallogin=sl)
    resolver.assert_not_called()
