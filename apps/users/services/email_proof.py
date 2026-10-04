"""Which email addresses a user has *proven* they own.

An email is proof of identity only when someone we trust verified it: allauth's
verified ``EmailAddress`` rows, or a sign-in from a provider that vouches for the
email it asserts. Account linking, user merges and workspace invites all key on
this, so an address a user merely typed in, or one an IdP passed along without
verifying, must never count.
"""

from __future__ import annotations

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.conf import settings

# Providers that report verification per login, through the OIDC ``email_verified``
# claim, instead of verifying every email they hand out (open-chat-studio#3647).
CLAIM_VERIFIED_PROVIDERS = frozenset({"ocs"})


def trusted_email_providers() -> set[str]:
    """Provider ids that verify every email they assert (CommCare HQ, Connect).

    Derived from ``SOCIALACCOUNT_PROVIDERS[<id>]["VERIFIED_EMAIL"] is True`` — the
    same setting that makes allauth mark those providers' emails verified.
    """
    providers = getattr(settings, "SOCIALACCOUNT_PROVIDERS", {}) or {}
    return {pid for pid, cfg in providers.items() if (cfg or {}).get("VERIFIED_EMAIL") is True}


def verified_social_email(provider: str, extra_data: dict | None) -> str | None:
    """The email a provider login asserted, if that provider vouched for it.

    ``extra_data`` is the provider's userinfo payload, as stored on (or about to be
    stored on) a ``SocialAccount``. allauth overwrites it on every login, so it
    reflects the provider's latest assertion.
    """
    data = extra_data or {}
    email = (data.get("email") or "").strip()
    if not email:
        return None
    if provider in trusted_email_providers():
        return email
    if provider in CLAIM_VERIFIED_PROVIDERS and data.get("email_verified") is True:
        return email
    return None


def proven_emails(user) -> set[str]:
    """Lowercased emails ``user`` has proven they own.

    Social-account assertions count alongside ``EmailAddress`` rows because some
    legitimate sign-ins never persist a verified row: an existing Connect or HQ
    identity whose email arrives on a later login, or an account created before
    those providers' emails were marked verified.
    """
    emails = {
        e.lower()
        for e in EmailAddress.objects.filter(user=user, verified=True).values_list(
            "email", flat=True
        )
    }
    for provider, extra_data in SocialAccount.objects.filter(user=user).values_list(
        "provider", "extra_data"
    ):
        email = verified_social_email(provider, extra_data)
        if email:
            emails.add(email.lower())
    return emails
