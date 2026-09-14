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
        return ["chatbots:read", "sessions:read", "files:read", "participants:read", "openid"]

    def extract_uid(self, data: dict) -> str:
        """Identify the (user, team) pair the token authorises, not just the user.

        An OCS access token is **team-scoped**, but the OIDC ``sub`` is the same
        for every team a user belongs to. allauth keys ``SocialAccount`` on
        ``(provider, uid)`` and ``SocialToken`` on ``(app, account)``, so a bare
        ``sub`` means authorising a second team overwrites the first team's token
        — the real reason multi-team support was impossible (#156), independently
        of the ``TenantConnection`` uniqueness constraint. Qualifying the uid with
        the ``team`` claim gives each team its own account and its own token.

        A response with no ``team`` claim keeps the bare ``sub``, so an OCS deploy
        that does not emit the claim behaves exactly as before.
        """
        sub = data.get("sub")
        if not sub:
            raise ValueError(f"Cannot determine UID from OCS userinfo response: {data!r}")
        team = str(data.get("team") or "").strip()
        return f"{sub}{UID_TEAM_SEPARATOR}{team}" if team else str(sub)

    def extract_common_fields(self, data: dict) -> dict:
        return {
            "email": data.get("email") or None,
            "username": data.get("preferred_username") or data.get("email") or "",
            "first_name": data.get("given_name", ""),
            "last_name": data.get("family_name", ""),
        }

    def extract_email_addresses(self, data: dict) -> list[EmailAddress]:
        """Return the user's email, trusted as verified only when OCS says so.

        Unlike CommCare HQ/Connect, OCS exposes per-login verification via the
        ``email_verified`` OIDC claim (open-chat-studio#3647). We mirror it rather
        than trusting wholesale: ``verified=True`` only when OCS asserts the claim.
        The claim is absent until that OCS change deploys, so this defaults closed
        (unverified) — never over-trusting an email OCS hasn't confirmed.
        """
        email = data.get("email") or None
        if not email:
            return []
        return [EmailAddress(email=email, verified=bool(data.get("email_verified")), primary=True)]


provider_classes = [OCSProvider]
