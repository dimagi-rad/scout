"""OAuth identity scope shared by resolution and credential lifecycle operations."""

from allauth.socialaccount.models import SocialAccount

from apps.users.providers.ocs.provider import team_slug_from_uid


def account_scope(account) -> str:
    if account is None or account.provider != "ocs":
        return ""
    # The qualified UID is allauth's identity key; prefer it over mutable claims.
    return (
        team_slug_from_uid(account.uid) or str((account.extra_data or {}).get("team") or "").strip()
    )


def scope_account_ids(user_id, provider, scope_key):
    return [
        account.pk
        for account in SocialAccount.objects.filter(user_id=user_id, provider=provider)
        if account_scope(account) == scope_key
    ]


def is_active_identity(account, bindings, *, provider=None):
    """Unbound scopes may bootstrap; an established binding owns its scope."""
    provider = provider or account.provider
    if provider.startswith("commcare_connect"):
        provider = "commcare_connect"
    elif provider.startswith("commcare"):
        provider = "commcare"
    return bindings.get((provider, account_scope(account))) in (None, account.pk)
