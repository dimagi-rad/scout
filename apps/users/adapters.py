"""
Custom allauth social account adapter with Fernet token encryption.

Encrypts the OAuth tokens allauth keeps in the session between the provider
redirect and the callback. The stored SocialToken rows are encrypted at rest by
apps.users.token_encryption. Both use the DB_CREDENTIAL_KEY Fernet key used for
project database credentials.
"""

from __future__ import annotations

import logging

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialToken
from allauth.socialaccount.providers import registry as providers_registry
from allauth.socialaccount.providers.base.constants import AuthError
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import (
    ImproperlyConfigured,
    MultipleObjectsReturned,
    ObjectDoesNotExist,
)
from django.shortcuts import redirect

from apps.common.commcare_servers import server_for_provider
from apps.users.providers.ocs.provider import REQUESTED_TEAM_STATE_KEY
from apps.users.services import ocs_team_flow
from apps.users.services.oauth_scope import canonical_provider, provider_accounts

logger = logging.getLogger(__name__)


def _signed_in_provider_id(request, sociallogin) -> str | None:
    """The allauth provider class id that performed this sign-in, if knowable."""
    provider = getattr(sociallogin, "provider", None)
    if provider is None:
        try:
            provider = sociallogin.account.get_provider(request)
        except (ObjectDoesNotExist, MultipleObjectsReturned, ImproperlyConfigured):
            # No single app names it (none, or one per server), so refuse.
            return None
    return getattr(provider, "id", None)


class EncryptingSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Adapter that Fernet-encrypts SocialToken fields in the login session."""

    def _get_fernet(self) -> Fernet:
        key = settings.DB_CREDENTIAL_KEY
        if not key:
            raise ValueError("DB_CREDENTIAL_KEY is not set in settings")
        return Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt_token(self, plaintext: str) -> str:
        """Encrypt a token string. Returns empty string for empty input."""
        if not plaintext:
            return ""
        f = self._get_fernet()
        return f.encrypt(plaintext.encode()).decode()

    def decrypt_token(self, ciphertext: str) -> str:
        """Decrypt a token string. Returns empty string for empty or unreadable input."""
        if not ciphertext:
            return ""
        f = self._get_fernet()
        try:
            return f.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            logger.exception(
                "Failed to decrypt OAuth token — possible key rotation or data corruption"
            )
            return ""

    def serialize_instance(self, instance):
        data = super().serialize_instance(instance)
        if isinstance(instance, SocialToken):
            if data.get("token"):
                data["token"] = self.encrypt_token(data["token"])
            if data.get("token_secret"):
                data["token_secret"] = self.encrypt_token(data["token_secret"])
        return data

    def deserialize_instance(self, model, data):
        if model is SocialToken:
            data = dict(data)  # don't mutate the original
            if data.get("token"):
                data["token"] = self.decrypt_token(data["token"])
            if data.get("token_secret"):
                data["token_secret"] = self.decrypt_token(data["token_secret"])
        return super().deserialize_instance(model, data)

    def pre_social_login(self, request, sociallogin):
        """Reject OAuth logins whose email is not in the per-provider allow-list.

        Runs after a successful OAuth callback but before any User/SocialAccount
        is created or login session established. Configured by the
        SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS setting (provider id -> list of
        allowed email domains). A provider without an entry inherits its canonical
        provider's, so a second CommCare HQ server (``commcare_eu``) cannot be a way
        around the ``commcare`` restriction; one with neither (or an empty list) is
        unrestricted.

        For a provider WITH a non-empty allow-list, a login that returns no email
        is rejected (arch #258, finding 07#2): a missing email must not silently
        bypass a configured domain restriction. Providers Scout deliberately
        leaves open (Connect, OCS) carry no allow-list, so their no-email logins
        are unaffected by this gate.
        """
        self._reject_cross_server_commcare_login(request, sociallogin)
        self._reject_ocs_team_mismatch(request, sociallogin)
        provider = sociallogin.account.provider
        restrictions = settings.SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS
        allowed = restrictions.get(provider, restrictions.get(canonical_provider(provider))) or []
        if not allowed:
            return

        allowed_lower = [d.lower() for d in allowed]
        email = (sociallogin.user.email or "").strip().lower()
        domain = email.rpartition("@")[2] if email else ""
        if domain and domain in allowed_lower:
            return

        provider_class = providers_registry.get_class(provider)
        provider_name = provider_class.name if provider_class else provider
        messages.error(
            request,
            "Sign-in with this account is not permitted. "
            f"Login using '{provider_name}' is restricted to {', '.join('@' + d for d in allowed_lower)} addresses.",
        )
        raise ImmediateHttpResponse(redirect("account_login"))

    def _reject_cross_server_commcare_login(self, request, sociallogin):
        """Refuse a CommCare sign-in whose stored id names another HQ server (#719).

        Scout reads a CommCare identity's server from its allauth provider id, but
        the token came from the adapter's server. An app configured under an alias
        that maps elsewhere (a ``commcare_eu*`` id on the www provider) would send
        that token to the wrong HQ, so it fails here instead of at first use.
        """
        stored_id = sociallogin.account.provider
        if canonical_provider(stored_id) != "commcare":
            return
        # allauth sets (and session-round-trips) the provider that signed in; a
        # CommCare login whose provider can't be found cannot be placed on a server,
        # so it fails closed.
        adapter_id = _signed_in_provider_id(request, sociallogin)
        if adapter_id and server_for_provider(adapter_id) == server_for_provider(stored_id):
            return
        logger.error(
            "CommCare sign-in refused: provider id %s maps to a different HQ server than %s",
            stored_id,
            adapter_id,
        )
        messages.error(request, "This CommCare HQ sign-in is misconfigured. Contact support.")
        raise ImmediateHttpResponse(redirect("account_login"))

    def _reject_ocs_team_mismatch(self, request, sociallogin):
        """Refuse an OCS token for a team other than the one Scout pinned the flow to.

        OCS falls back to the browser's session team when the pinned slug is not one
        of the user's teams, and says so only through the ``team`` claim. Connecting
        that token anyway would quietly add a team the user did not pick.
        """
        if canonical_provider(sociallogin.account.provider) != "ocs":
            return
        requested = (sociallogin.state or {}).get(REQUESTED_TEAM_STATE_KEY)
        if not requested:
            return
        claims = sociallogin.account.extra_data or {}
        got = str(claims.get("team") or "").strip()
        if got == requested:
            return
        logger.warning("OCS returned team %r for a flow pinned to %r; refused", got, requested)
        request.session[ocs_team_flow.SESSION_KEY] = ocs_team_flow.stop(
            request.session.get(ocs_team_flow.SESSION_KEY),
            ocs_team_flow.STOP_MISMATCH,
            requested,
            got,
        )
        self._store_fresh_ocs_teams(request, claims)
        raise ImmediateHttpResponse(redirect(self._connections_url(request)))

    def _store_fresh_ocs_teams(self, request, claims):
        # The refused login never reaches allauth's extra_data update, but its team
        # list is the freshest one; without it a team the user left keeps being offered.
        teams = ocs_team_flow.teams_from_claims(claims)
        sub = str(claims.get("sub") or "")
        if teams is None or not sub or not request.user.is_authenticated:
            return
        for account in provider_accounts(request.user.pk, "ocs"):
            if ocs_team_flow.subject_of(account) == sub:
                account.extra_data = {**(account.extra_data or {}), "teams": teams}
                account.save(update_fields=["extra_data"])

    def on_authentication_error(
        self, request, provider, error=None, exception=None, extra_context=None
    ):
        """Bring a failed or cancelled pinned OCS connect back to the connections page.

        Only flows Scout pinned to a team are redirected; any other failure keeps
        allauth's own error page.
        """
        super().on_authentication_error(
            request, provider, error=error, exception=exception, extra_context=extra_context
        )
        state = (extra_context or {}).get("state") or {}
        requested = state.get(REQUESTED_TEAM_STATE_KEY) if isinstance(state, dict) else None
        if not requested or canonical_provider(getattr(provider, "id", "")) != "ocs":
            return
        reason = (
            ocs_team_flow.STOP_CANCELLED
            if error == AuthError.CANCELLED
            else ocs_team_flow.STOP_FAILED
        )
        request.session[ocs_team_flow.SESSION_KEY] = ocs_team_flow.stop(
            request.session.get(ocs_team_flow.SESSION_KEY), reason, requested
        )
        raise ImmediateHttpResponse(redirect(self._connections_url(request)))

    def _connections_url(self, request):
        script_name = request.META.get("SCRIPT_NAME", "").rstrip("/")
        return f"{script_name}/settings/connections"

    def get_connect_redirect_url(self, request, socialaccount):
        """Where allauth sends the browser after a ``?process=connect`` round-trip.

        allauth's default reverses ``socialaccount_connections``, a name that
        lives in ``allauth.socialaccount.urls`` — which Scout deliberately does
        NOT mount (see apps/users/allauth_urls.py). That reverse raises
        NoReverseMatch and, because allauth evaluates it eagerly (before the
        ``or sociallogin.get_redirect_url(...)`` fallback), the connect flow 500s
        even when the SPA passes a valid ``?next=`` (prod SCOUT-DJANGO-25).

        Point at the SPA connections page instead, honoring any mount prefix
        (FORCE_SCRIPT_NAME → SCRIPT_NAME in request meta) the same way the
        artifact sandbox does.
        """
        return self._connections_url(request)


def encrypt_credential(plaintext: str) -> str:
    """Fernet-encrypt a credential string using DB_CREDENTIAL_KEY."""
    key = settings.DB_CREDENTIAL_KEY
    if not key:
        raise ValueError("DB_CREDENTIAL_KEY is not set in settings")
    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.encrypt(plaintext.encode()).decode()


def decrypt_credential(ciphertext: str) -> str:
    """Fernet-decrypt a credential string using DB_CREDENTIAL_KEY."""
    key = settings.DB_CREDENTIAL_KEY
    if not key:
        raise ValueError("DB_CREDENTIAL_KEY is not set in settings")
    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.decrypt(ciphertext.encode()).decode()
