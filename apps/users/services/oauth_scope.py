"""OAuth identity scope shared by resolution and credential lifecycle operations."""

from allauth.socialaccount.models import SocialAccount

from apps.users.providers.ocs.provider import team_slug_from_uid


def canonical_provider(provider: str) -> str:
    for key in ("commcare_connect", "commcare", "ocs"):
        if provider.startswith(key):
            return key
    return provider


def provider_accounts(user_id, provider):
    provider = canonical_provider(provider)
    accounts = SocialAccount.objects.filter(user_id=user_id)
    if provider in ("commcare", "commcare_connect", "ocs"):
        accounts = accounts.filter(provider__startswith=provider)
        if provider == "commcare":
            accounts = accounts.exclude(provider__startswith="commcare_connect")
        return accounts
    return accounts.filter(provider=provider)


def account_scope(account) -> str:
    if account is None or canonical_provider(account.provider) != "ocs":
        return ""
    # The qualified UID is allauth's identity key; prefer it over mutable claims.
    return (
        team_slug_from_uid(account.uid) or str((account.extra_data or {}).get("team") or "").strip()
    )


def oauth_membership_scope_mismatch(membership, connection, account) -> bool:
    """Return whether a known membership scope conflicts with its OAuth identity.

    The immutable connection scope takes precedence over the legacy account
    claim. Keep the raw comparison aligned with runtime credential resolution:
    stored whitespace or other malformed scope data must fail closed.
    """
    if not membership.team_slug:
        return False
    current = connection.scope_key or (getattr(account, "extra_data", None) or {}).get("team")
    return bool(current) and current != membership.team_slug


def scope_account_ids(user_id, provider, scope_key):
    return [
        account.pk
        for account in provider_accounts(user_id, provider)
        if account_scope(account) == scope_key
    ]


def is_active_identity(account, bindings, *, provider=None):
    """Unbound scopes may bootstrap; an established binding owns its scope."""
    provider = canonical_provider(provider or account.provider)
    return bindings.get((provider, account_scope(account))) in (None, account.pk)
