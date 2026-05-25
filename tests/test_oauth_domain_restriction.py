"""Tests for OAuth email-domain restriction enforcement and configuration."""

from django.conf import settings


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
