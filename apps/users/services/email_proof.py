"""Which email addresses a user has *proven* they own.

An email is proof of identity only when someone we trust verified it: allauth's
verified ``EmailAddress`` rows, or a sign-in from a provider that vouches for the
email it asserts. Account linking, user merges and workspace invites all key on
this, so an address a user merely typed in, or one an IdP passed along without
verifying, must never count.
"""

from __future__ import annotations

from collections.abc import Iterable

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialApp
from allauth.socialaccount.providers import registry
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


def provider_class_ids(provider_ids: Iterable[str]) -> dict[str, str | None]:
    """Map stored ``SocialAccount.provider`` values to provider class ids.

    A ``SocialApp`` with a ``provider_id`` (``hq_production``, ``ocs_staging``)
    stores its accounts under that alias, while allauth keys provider settings by
    the class id. An id no app claims resolves only if it is itself a class id, so
    an orphaned or ambiguous alias maps to None and vouches for nothing.
    """
    provider_ids = set(provider_ids)
    classes: dict[str, set[str]] = {}
    for alias, cls in SocialApp.objects.filter(provider_id__in=provider_ids).values_list(
        "provider_id", "provider"
    ):
        classes.setdefault(alias, set()).add(cls)
    known = {cls.id for cls in registry.get_class_list()}
    resolved: dict[str, str | None] = {}
    for pid in provider_ids:
        claimed = classes.get(pid)
        if claimed:
            resolved[pid] = next(iter(claimed)) if len(claimed) == 1 else None
        else:
            resolved[pid] = pid if pid in known else None
    return resolved


def verified_social_email(
    provider: str,
    extra_data: dict | None,
    *,
    class_id: str | None = None,
    trusted: set[str] | None = None,
) -> str | None:
    """The email a provider login asserted, if that provider vouched for it.

    ``extra_data`` is the provider's userinfo payload, as stored on (or about to be
    stored on) a ``SocialAccount``. allauth overwrites it on every login, so it
    reflects the provider's latest assertion. ``class_id`` and ``trusted`` let a
    caller checking many accounts resolve them once.
    """
    if not isinstance(extra_data, dict):
        return None
    email = extra_data.get("email")
    if not isinstance(email, str) or not email.strip():
        return None
    email = email.strip()
    if class_id is None:
        class_id = provider_class_ids([provider])[provider]
    if trusted is None:
        trusted = trusted_email_providers()
    # A claim-verified provider is never blanket-trusted, even if a settings edit
    # gives it VERIFIED_EMAIL again.
    if class_id in trusted - CLAIM_VERIFIED_PROVIDERS:
        return email
    if class_id in CLAIM_VERIFIED_PROVIDERS and extra_data.get("email_verified") is True:
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
