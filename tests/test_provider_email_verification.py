"""Provider ``extract_email_addresses``: trusted IdPs assert verified emails.

allauth's base ``extract_email_addresses`` returns ``[]``, which makes allauth
fall back to an *unverified* EmailAddress. That unverified state is what made the
cross-provider account merge refuse to reconcile, stranding email-less duplicate
accounts. Each Scout provider now asserts verification per its upstream IdP:
CommCare HQ and Connect verify emails themselves (trusted unconditionally); OCS
exposes an ``email_verified`` OIDC claim that we mirror (default closed).
"""

import pytest
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialApp

from apps.users.providers.commcare.provider import CommCareProvider
from apps.users.providers.commcare_connect.provider import CommCareConnectProvider
from apps.users.providers.ocs.provider import OCSProvider


def _addresses(provider_cls, data: dict) -> list[EmailAddress]:
    # allauth's provider __init__ requires a non-None app, but
    # extract_email_addresses only reads ``data`` — an unsaved SocialApp
    # instance suffices, so these stay pure unit tests (no DB).
    app = SocialApp(provider=provider_cls.id, name="test", client_id="x", secret="x")
    return provider_cls(request=None, app=app).extract_email_addresses(data)


@pytest.mark.parametrize(
    "provider_cls",
    [CommCareProvider, CommCareConnectProvider],
    ids=["commcare", "commcare_connect"],
)
def test_trusted_provider_marks_email_verified(provider_cls):
    addrs = _addresses(provider_cls, {"email": "user@dimagi.com"})
    assert len(addrs) == 1
    assert addrs[0].email == "user@dimagi.com"
    assert addrs[0].verified is True
    assert addrs[0].primary is True


@pytest.mark.parametrize(
    "provider_cls, data",
    [
        (CommCareProvider, {}),
        (CommCareProvider, {"email": ""}),
        (CommCareConnectProvider, {"email": None}),
        (CommCareConnectProvider, {"name": "Ajeet"}),
        (OCSProvider, {"email_verified": True}),
    ],
    ids=["cc-missing", "cc-empty", "connect-none", "connect-no-email", "ocs-no-email"],
)
def test_no_email_returns_empty(provider_cls, data):
    assert _addresses(provider_cls, data) == []


def test_ocs_trusts_email_only_when_claim_true():
    addrs = _addresses(OCSProvider, {"email": "user@dimagi.com", "email_verified": True})
    assert len(addrs) == 1
    assert addrs[0].email == "user@dimagi.com"
    assert addrs[0].verified is True
    assert addrs[0].primary is True


@pytest.mark.parametrize(
    "data",
    [
        {"email": "user@dimagi.com", "email_verified": False},
        {"email": "user@dimagi.com"},  # claim absent (pre open-chat-studio#3647 deploy)
    ],
    ids=["claim-false", "claim-absent"],
)
def test_ocs_email_unverified_without_true_claim(data):
    addrs = _addresses(OCSProvider, data)
    assert len(addrs) == 1
    assert addrs[0].verified is False
