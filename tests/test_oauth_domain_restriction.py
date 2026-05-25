"""Tests for OAuth email-domain restriction enforcement and configuration."""

import pytest
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.conf import settings
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, override_settings

from apps.users.adapters import EncryptingSocialAccountAdapter
from apps.users.models import User


class TestAllowedEmailDomainsSetting:
    """Verify the SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS setting shape and defaults."""

    def test_setting_exists_and_is_dict(self):
        assert isinstance(settings.SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS, dict)

    def test_default_restricts_all_five_providers_to_dimagi_com(self):
        expected_providers = {"google", "github", "commcare", "commcare_connect", "ocs"}
        actual = settings.SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS
        assert set(actual.keys()) == expected_providers
        for provider, domains in actual.items():
            assert domains == ["dimagi.com"], f"{provider} default should be ['dimagi.com']"


def _make_request():
    """Build a request with the messages framework wired in."""
    request = RequestFactory().get("/accounts/google/login/callback/")
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


def _make_sociallogin(provider: str, email: str) -> SocialLogin:
    """Build an in-memory SocialLogin for adapter testing (no DB writes)."""
    user = User(email=email)
    account = SocialAccount(provider=provider, uid="test-uid")
    sociallogin = SocialLogin(user=user, account=account)
    return sociallogin


class TestPreSocialLoginEnforcement:
    """Test the adapter's pre_social_login domain-restriction logic."""

    @pytest.fixture
    def adapter(self):
        return EncryptingSocialAccountAdapter()

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={"google": ["dimagi.com"]})
    def test_allowed_domain_passes(self, adapter):
        request = _make_request()
        sociallogin = _make_sociallogin("google", "alice@dimagi.com")
        # Should not raise.
        assert adapter.pre_social_login(request, sociallogin) is None

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={"google": ["dimagi.com"]})
    def test_disallowed_domain_blocked(self, adapter):
        request = _make_request()
        sociallogin = _make_sociallogin("google", "alice@example.com")
        with pytest.raises(ImmediateHttpResponse) as exc_info:
            adapter.pre_social_login(request, sociallogin)
        response = exc_info.value.response
        assert response.status_code == 302
        assert response.url == "/accounts/login/"
        # An error message should be queued for the user.
        messages = list(request._messages)
        assert len(messages) == 1
        assert "not permitted" in messages[0].message.lower()
        assert "@dimagi.com" in messages[0].message

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={"google": ["dimagi.com"]})
    def test_empty_email_allowed(self, adapter):
        request = _make_request()
        sociallogin = _make_sociallogin("google", "")
        assert adapter.pre_social_login(request, sociallogin) is None

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={})
    def test_unrestricted_provider_allowed(self, adapter):
        request = _make_request()
        sociallogin = _make_sociallogin("commcare_connect", "user@anything.com")
        assert adapter.pre_social_login(request, sociallogin) is None

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={"google": ["dimagi.com"]})
    def test_provider_not_in_allowlist_is_unrestricted(self, adapter):
        request = _make_request()
        sociallogin = _make_sociallogin("github", "user@example.com")
        assert adapter.pre_social_login(request, sociallogin) is None

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={"google": ["dimagi.com"]})
    def test_case_insensitive_match(self, adapter):
        request = _make_request()
        sociallogin = _make_sociallogin("google", "Alice@DIMAGI.COM")
        assert adapter.pre_social_login(request, sociallogin) is None

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={"google": ["dimagi.com", "dimagi.org"]})
    def test_multiple_allowed_domains(self, adapter):
        # First domain matches.
        assert (
            adapter.pre_social_login(_make_request(), _make_sociallogin("google", "a@dimagi.com"))
            is None
        )
        # Second domain matches.
        assert (
            adapter.pre_social_login(_make_request(), _make_sociallogin("google", "b@dimagi.org"))
            is None
        )
        # Third domain blocked.
        with pytest.raises(ImmediateHttpResponse):
            adapter.pre_social_login(_make_request(), _make_sociallogin("google", "c@other.com"))

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={"google": []})
    def test_empty_allow_list_means_unrestricted(self, adapter):
        request = _make_request()
        sociallogin = _make_sociallogin("google", "user@anything.com")
        assert adapter.pre_social_login(request, sociallogin) is None
