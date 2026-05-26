"""Pin the auth settings that enable cross-provider account linking by email."""

from django.conf import settings


def test_email_authentication_is_enabled():
    assert settings.SOCIALACCOUNT_EMAIL_AUTHENTICATION is True


def test_email_authentication_auto_connect_is_enabled():
    assert settings.SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT is True


def test_dimagi_providers_have_verified_email():
    providers = settings.SOCIALACCOUNT_PROVIDERS
    for pid in ("commcare", "commcare_connect", "ocs"):
        assert providers[pid].get("VERIFIED_EMAIL") is True, (
            f"{pid} must declare VERIFIED_EMAIL=True so allauth treats its emails as verified"
        )
