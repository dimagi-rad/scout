"""OAuth2 provider for Open Chat Studio (OCS)."""

from __future__ import annotations

from allauth.account.models import EmailAddress
from allauth.socialaccount.providers.base import ProviderAccount
from allauth.socialaccount.providers.oauth2.provider import OAuth2Provider

from apps.users.providers.ocs.views import OCSOAuth2Adapter

# Separates the OIDC subject from the team slug in a SocialAccount uid. Not a
# character a slug or a subject can contain, so the split is unambiguous.
UID_TEAM_SEPARATOR = "#"


def team_slug_from_uid(uid: str) -> str:
    """The team slug encoded in an OCS ``SocialAccount.uid``, or "" if unqualified."""
    _sub, _sep, team = (uid or "").partition(UID_TEAM_SEPARATOR)
    return team


def _verified_email(data: dict) -> str | None:
    if data.get("email_verified") is not True:
        return None
    return data.get("email") or None


class OCSAccount(ProviderAccount):
    def get_avatar_url(self) -> str | None:
        return None

    def to_str(self) -> str:
        return self.account.extra_data.get("username", super().to_str())


class OCSProvider(OAuth2Provider):
    id = "ocs"
    name = "Open Chat Studio"
    account_class = OCSAccount
    oauth2_adapter_class = OCSOAuth2Adapter

    def get_default_scope(self) -> list[str]:
        # "teams" lists every team the user belongs to (open-chat-studio#4685).
        return [
            "chatbots:read",
            "sessions:read",
            "files:read",
            "participants:read",
            "openid",
            "teams",
        ]

    def extract_uid(self, data: dict) -> str:
        """Identify the (user, team) pair the token authorises, not just the user.

        An OCS access token is **team-scoped**, but the OIDC ``sub`` is the same
        for every team a user belongs to. allauth keys ``SocialAccount`` on
        ``(provider, uid)`` and ``SocialToken`` on ``(app, account)``, so a bare
        ``sub`` means authorising a second team overwrites the first team's token
        — the real reason multi-team support was impossible (#156), independently
        of the ``TenantConnection`` uniqueness constraint. Qualifying the uid with
        the ``team`` claim gives each team its own account and its own token.

        A response with no ``team`` claim keeps the bare ``sub``, so sign-in still
        works on an OCS deploy that does not emit the claim, but under all-of access
        such an identity discovers no chatbots (``resolve_ocs_chatbots``, #379).
        """
        sub = data.get("sub")
        if not sub:
            raise ValueError(f"Cannot determine UID from OCS userinfo response: {data!r}")
        team = str(data.get("team") or "").strip()
        return f"{sub}{UID_TEAM_SEPARATOR}{team}" if team else str(sub)

    def extract_common_fields(self, data: dict) -> dict:
        return {
            "email": _verified_email(data),
            "username": data.get("preferred_username") or _verified_email(data) or "",
            "first_name": data.get("given_name", ""),
            "last_name": data.get("family_name", ""),
        }

    def extract_email_addresses(self, data: dict) -> list[EmailAddress]:
        """Return the user's email only when OCS asserts it verified.

        Unlike CommCare HQ/Connect, OCS reports verification per login via the
        ``email_verified`` OIDC claim (open-chat-studio#3647). An unverified email
        is dropped rather than kept as unverified: allauth would still set it as
        ``User.email``, and a collision with an existing account sends the login to
        the social signup form, which Scout does not mount. Without it, the login
        proceeds as an email-less identity, like a Connect user with no email.
        """
        email = _verified_email(data)
        if not email:
            return []
        return [EmailAddress(email=email, verified=True, primary=True)]


provider_classes = [OCSProvider]
