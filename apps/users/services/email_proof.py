"""Which email addresses a user has *proven* they own.

An email is proof of identity only when someone we trust verified it: allauth's
verified ``EmailAddress`` rows, or a sign-in from a provider that vouches for the
email it asserts. Account linking, user merges and workspace invites all key on
this, so an address a user merely typed in, or one an IdP passed along without
verifying, must never count.
"""

from __future__ import annotations

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialApp
from django.conf import settings

from apps.users.services.oauth_scope import canonical_provider

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


def provider_class_ids(provider_ids) -> dict[str, str]:
    """Map stored ``SocialAccount.provider`` values to provider class ids.

    A ``SocialApp`` with a ``provider_id`` (``hq_production``, ``ocs_staging``)
    stores its accounts under that alias, while allauth keys provider settings by
    the class id. An id no single app claims falls back to its prefix.
    """
    provider_ids = set(provider_ids)
    classes: dict[str, set[str]] = {}
    for alias, cls in SocialApp.objects.filter(provider_id__in=provider_ids).values_list(
        "provider_id", "provider"
    ):
        classes.setdefault(alias, set()).add(cls)
    return {
        pid: next(iter(classes[pid])) if len(classes.get(pid, ())) == 1 else canonical_provider(pid)
        for pid in provider_ids
    }


def verified_social_email(
    provider: str, extra_data: dict | None, *, class_id: str | None = None, trusted=None
) -> str | None:
    """The email a provider login asserted, if that provider vouched for it.

    ``extra_data`` is the provider's userinfo payload, as stored on (or about to be
    stored on) a ``SocialAccount``. allauth overwrites it on every login, so it
    reflects the provider's latest assertion. ``class_id`` and ``trusted`` let a
    caller checking many accounts resolve them once.
    """
    data = extra_data or {}
    email = data.get("email")
    if not isinstance(email, str) or not email.strip():
        return None
    email = email.strip()
    if class_id is None:
        class_id = provider_class_ids([provider])[provider]
    if trusted is None:
        trusted = trusted_email_providers()
    if class_id in trusted:
        return email
    if class_id in CLAIM_VERIFIED_PROVIDERS and data.get("email_verified") is True:
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
    accounts = list(SocialAccount.objects.filter(user=user).values_list("provider", "extra_data"))
    if not accounts:
        return emails
    class_ids = provider_class_ids(provider for provider, _ in accounts)
    trusted = trusted_email_providers()
    for provider, extra_data in accounts:
        email = verified_social_email(
            provider, extra_data, class_id=class_ids[provider], trusted=trusted
        )
        if email:
            emails.add(email.lower())
    return emails
