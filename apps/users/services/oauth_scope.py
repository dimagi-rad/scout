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
