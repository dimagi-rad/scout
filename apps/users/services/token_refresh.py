"""OAuth token refresh service.

Handles refreshing expired OAuth tokens for the OAuth providers.

Two entry points:

- ``refresh_oauth_token`` (async): the proactive path. The credential resolver
  renews a near-expiry token at task start, and ``providers_view`` renews on poll.
- ``refresh_oauth_token_sync`` (sync): the reactive path. A loader running under
  ``asyncio.to_thread`` calls it on a mid-run 401 to renew a token whose lifetime
  is shorter than the run (CommCare's 15-min OAuth TTL) without bridging back to
  the event loop (arch #252, findings 14#3/14#4).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum

import httpx
import requests
from allauth.socialaccount.models import SocialAccount, SocialToken
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import DatabaseError, transaction
from django.db import connection as django_connection
from django.db.models import Q
from django.utils import timezone

from apps.common.db_deadline import preserve_transaction_timeouts
from apps.common.error_codes import ErrorCode
from apps.common.errors import TokenRefreshError, UpstreamRefreshFailed, UpstreamTokenExpired
from apps.users.models import TenantConnection, User
from apps.users.services.oauth_scope import account_scope, canonical_provider

logger = logging.getLogger(__name__)

# Refresh tokens that expire within this window
REFRESH_BUFFER = timedelta(minutes=5)

#: Somebody is watching a page, and providers_view refreshes once per connected token,
#: so this is a per-token slice of a page budget rather than a whole one.
INTERACTIVE_DB_DEADLINE = 5.0

#: Matches the 30s request_timeout the refresh wrappers already allow one provider call:
#: the module's established unit of patience for a single refresh attempt. A worker is
#: not waiting on a person, but blocking forever stalls thread-sensitive ORM work for
#: everything else sharing the executor.
WORKER_DB_DEADLINE = 30.0


def _ocs_token_url() -> str:
    return f"{settings.OCS_URL.rstrip('/')}/o/token/"


def _connect_token_url() -> str:
    return f"{settings.CONNECT_API_URL.rstrip('/')}/o/token/"


PROVIDER_TOKEN_URLS = {
    "commcare": "https://www.commcarehq.org/oauth/token/",
}


def get_token_url(provider: str) -> str | None:
    """Return the OAuth token endpoint for a provider, or None if unknown."""
    provider = canonical_provider(provider)
    if provider == "ocs":
        return _ocs_token_url()
    if provider == "commcare_connect":
        return _connect_token_url()
    return PROVIDER_TOKEN_URLS.get(provider)


class TokenRefreshUnavailable(TokenRefreshError, UpstreamRefreshFailed):
    """An HTTP or transport failure prevented credential refresh."""


class TokenRefreshDeadlineExceeded(TokenRefreshUnavailable):
    """The database wait for a refresh outran its budget before the grant was spent.

    Retrying is the right move -- the lock holder will have moved on -- so this keeps
    the retryable classification. It is a distinct class only so a contended database
    is never read as an unexplained refresh failure.
    """


class TokenRefreshRejected(TokenRefreshError, UpstreamTokenExpired):
    """The provider explicitly rejected the refresh grant as invalid."""

    code = ErrorCode.AUTH_TOKEN_EXPIRED
    # A dead refresh grant requires reconnect, but does not prove resource access loss.
    denial_handled = True


class TokenRefreshStatus(StrEnum):
    APPLIED = "applied"
    SUPERSEDED = "superseded"


class _RefreshDeadlineExceeded(TimeoutError):
    pass


@dataclass(frozen=True)
class PersistedTokenSnapshot:
    token_id: int
    account_id: int
    app_id: int
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: timezone.datetime | None


@dataclass(frozen=True)
class TokenRefreshResult:
    status: TokenRefreshStatus
    snapshot: PersistedTokenSnapshot
    #: On SUPERSEDED, whether the stored credential moved on from what preflight saw.
    #: False means a concurrent writer changed the fences but not the token, so the
    #: snapshot still holds the credential this refresh just rotated away upstream.
    credential_advanced: bool = True


@dataclass(frozen=True)
class _TokenPreflight:
    token_id: int
    account_id: int
    app_id: int
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: timezone.datetime | None
    user_id: int
    account_provider: str
    account_scope: str
    connection_fences: tuple[_ConnectionFence, ...]


@dataclass(frozen=True)
class _ConnectionFence:
    """The tenant binding a refresh was authorised against.

    Only fields whose change would make the new credential land on a different
    principal belong here. Volatile operational state -- notably
    ``upstream_denied_at``, which ``record_validated_upstream_denial`` sets on any
    single-tenant 403 -- must stay out: a denial arriving during the HTTP round
    trip is routine, and vetoing on it discards a token pair the provider has
    already issued and rotated away.
    """

    connection_id: object
    user_id: int
    provider: str
    credential_type: str
    social_account_id: int | None
    scope_key: str
    upstream_denied_at: timezone.datetime | None

    @property
    def identity(self) -> tuple:
        """The binding itself, without the operational state layered on top of it."""
        return (
            self.connection_id,
            self.user_id,
            self.provider,
            self.credential_type,
            self.social_account_id,
            self.scope_key,
        )


@dataclass(frozen=True)
class _ValidatedRefresh:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: timezone.datetime | None


#: OAuth 2 names its token-endpoint failures in a fixed set (RFC 6749 5.2). The code
#: is an enum, not a secret, so it is safe to log even though the body is not -- and it
#: is the only thing separating our own misconfiguration (invalid_client) from a routine
#: dead grant (invalid_grant). Anything unrecognised is withheld rather than echoed.
def _reports_oauth_error(response, code: str) -> bool:
    """Does the provider's body name this specific OAuth error?"""
    if response is None:
        return False
    try:
        data = response.json()
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("error") == code


def _is_invalid_grant(response) -> bool:
    if response is None or response.status_code not in (400, 401, 403):
        return False
    return _reports_oauth_error(response, "invalid_grant")


def _is_invalid_client(response) -> bool:
    """Our own client credentials were rejected -- a deployment fault, not a user's."""
    if response is None or response.status_code not in (400, 401, 403):
        return False
    return _reports_oauth_error(response, "invalid_client")


def token_needs_refresh(expires_at: timezone.datetime | None, *, can_refresh: bool = True) -> bool:
    """Check if a token needs refreshing based on its expiry time.

    Returns True if the token expires within REFRESH_BUFFER.

    An **unknown** expiry (``None``) used to return False — "assume valid" — which
    fails open. ``expires_at`` is the only health signal Scout has, so a revoked
    token with no recorded expiry was never proactively refreshed, was handed
    straight to the loaders, and reported "connected" in the UI right up until it
    401'd (#373). Unknown expiry now counts as needing a refresh, which is the one
    action that can actually establish whether the credential is still alive.

    ``can_refresh`` guards that: with no refresh token there is nothing to
    attempt, and returning True would only condemn a credential that may well be
    working. So unknown-expiry-and-unrefreshable keeps the old answer, and its
    honesty problem is fixed where it is visible instead — ``providers_view``
    reports such a token as needing reconnection rather than asserting it is fine.
    """
    if expires_at is None:
        return can_refresh
    return timezone.now() + REFRESH_BUFFER >= expires_at


def credential_fingerprint(token) -> str:
    value = json.dumps([token.token, token.token_secret, token.app_id])
    return hashlib.sha256(value.encode()).hexdigest()


def token_health(token, provider, *, refresh_failed=False) -> str:
    """User-action status, independent of the proactive refresh buffer."""
    if refresh_failed:
        return "expired"
    token_url = get_token_url(provider)
    if token_url and token.token_secret and token.app:
        return "connected"
    if token.expires_at is not None:
        return "connected" if token.expires_at > timezone.now() else "expired"
    return "expired" if token_url else "connected"


def _configure_transaction_deadline(deadline, clock) -> None:
    if deadline is None:
        return
    remaining = deadline - clock()
    if remaining <= 0:
        raise _RefreshDeadlineExceeded
    if django_connection.vendor == "postgresql":
        milliseconds = max(1, int(remaining * 1000))
        with django_connection.cursor() as cursor:
            cursor.execute("SELECT set_config('lock_timeout', %s, true)", [f"{milliseconds}ms"])
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)", [f"{milliseconds}ms"]
            )


#: PostgreSQL's codes for a statement cancelled by ``statement_timeout`` and a lock
#: refused by ``lock_timeout``. Reading the SQLSTATE of a driver we do not own is the
#: boundary-adapter case that ``apps/common/errors.py`` explicitly allows; the
#: alternative is inferring a timeout from prose.
_DEADLINE_SQLSTATES = frozenset({"57014", "55P03"})


def _phase_deadline(deadline, db_timeout, clock):
    """The budget for one database phase.

    ``db_timeout`` bounds a single database phase and is re-armed for each, because
    the provider round trip sits between them: carrying one deadline across it let a
    slow-but-successful provider spend the budget meant for the write, so a healthy
    idle database "timed out" and a freshly rotated credential was discarded.

    An explicit ``deadline`` is an end-to-end contract from the caller and is honoured
    exactly as given.
    """
    if deadline is not None:
        return deadline
    if db_timeout is None:
        return None
    return clock() + db_timeout


def _grant_was_spent(preflight: _TokenPreflight, refreshed: _ValidatedRefresh) -> bool:
    """Did the provider rotate the refresh token, invalidating what we hold?

    ``_validate_refresh_response`` falls back to the stored refresh token when the
    response omits one, so an unchanged value means the provider did not rotate and
    the stored credential is still refreshable.
    """
    return refreshed.refresh_token != preflight.refresh_token


def _is_deadline_error(exc: BaseException) -> bool:
    """True when this refresh's own budget fired, rather than the database faulting.

    The explicit clock checks raise ``_RefreshDeadlineExceeded``, but the waits that
    matter are enforced by PostgreSQL and arrive as a wrapped driver error.
    """
    if isinstance(exc, _RefreshDeadlineExceeded):
        return True
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate is None:
        sqlstate = getattr(exc.__cause__, "sqlstate", None)
    return sqlstate in _DEADLINE_SQLSTATES


def _ensure_before_deadline(deadline, clock) -> None:
    if deadline is not None and clock() >= deadline:
        raise _RefreshDeadlineExceeded


def _preflight_token(token, *, deadline=None, clock=time.monotonic) -> _TokenPreflight:
    with transaction.atomic(), preserve_transaction_timeouts():
        _configure_transaction_deadline(deadline, clock)
        account = SocialAccount.objects.get(pk=token.account_id)
        _configure_transaction_deadline(deadline, clock)
        connection_fences = tuple(
            _ConnectionFence(
                connection_id=row["id"],
                user_id=row["user_id"],
                provider=row["provider"],
                credential_type=row["credential_type"],
                social_account_id=row["social_account_id"],
                scope_key=row["scope_key"],
                upstream_denied_at=row["upstream_denied_at"],
            )
            for row in TenantConnection.objects.filter(
                social_account_id=token.account_id,
                credential_type=TenantConnection.OAUTH,
            )
            .order_by("pk")
            .values(
                "id",
                "user_id",
                "provider",
                "credential_type",
                "social_account_id",
                "scope_key",
                "upstream_denied_at",
            )
        )
        _ensure_before_deadline(deadline, clock)
    preflight = _TokenPreflight(
        token_id=token.pk,
        account_id=token.account_id,
        app_id=token.app_id,
        access_token=token.token,
        refresh_token=token.token_secret,
        expires_at=token.expires_at,
        user_id=account.user_id,
        account_provider=canonical_provider(account.provider),
        account_scope=account_scope(account),
        connection_fences=connection_fences,
    )
    if not all(
        fence.user_id == preflight.user_id
        and canonical_provider(fence.provider) == preflight.account_provider
        and fence.scope_key == preflight.account_scope
        for fence in connection_fences
    ):
        raise TokenRefreshError(
            "OAuth connection identity is inconsistent; reconnect this account."
        )
    return preflight


def _connection_fence(connection) -> _ConnectionFence:
    return _ConnectionFence(
        connection_id=connection.id,
        user_id=connection.user_id,
        provider=connection.provider,
        credential_type=connection.credential_type,
        social_account_id=connection.social_account_id,
        scope_key=connection.scope_key,
        upstream_denied_at=connection.upstream_denied_at,
    )


def _lock_refresh_context(preflight: _TokenPreflight, *, deadline=None, clock=time.monotonic):
    _configure_transaction_deadline(deadline, clock)
    User.objects.select_for_update().get(pk=preflight.user_id)
    _configure_transaction_deadline(deadline, clock)
    current = (
        SocialToken.objects.select_for_update(of=("self",))
        .select_related("account")
        .get(pk=preflight.token_id)
    )
    observed_ids = [fence.connection_id for fence in preflight.connection_fences]
    _configure_transaction_deadline(deadline, clock)
    connections = list(
        TenantConnection.objects.select_for_update()
        .filter(
            Q(social_account_id=preflight.account_id, credential_type=TenantConnection.OAUTH)
            | Q(pk__in=observed_ids)
        )
        .order_by("pk")
    )
    return current, connections


def _identity_matches(
    preflight: _TokenPreflight, current: SocialToken, connections: list[TenantConnection]
) -> bool:
    """Does this row still describe the binding the caller validated at preflight?

    Single-sourced because two callers need the same verdict: the CAS, which must not
    write, and the read-back on a CAS miss, which must not *serve* a credential from a
    principal the caller never saw.
    """
    if (
        current.account_id != preflight.account_id
        or current.app_id != preflight.app_id
        or current.account.user_id != preflight.user_id
        or canonical_provider(current.account.provider) != preflight.account_provider
        or account_scope(current.account) != preflight.account_scope
    ):
        return False
    current_fences = {connection.id: _connection_fence(connection) for connection in connections}
    for fence in preflight.connection_fences:
        # A connection that appeared or was removed mid-flight is not this refresh's
        # business; one that was re-pointed at a different principal is.
        observed = current_fences.get(fence.connection_id)
        if observed is not None and observed.identity != fence.identity:
            return False
    return True


def _refresh_context_matches(
    preflight: _TokenPreflight,
    current: SocialToken,
    connections: list[TenantConnection],
    *,
    fence_denial: bool,
) -> bool:
    """Has anything changed that should stop this write?

    ``fence_denial`` splits the two callers. Writing a *failure marker* must not
    clobber a denial that landed while we were away, so that path fences on it.
    Writing a *refreshed credential* must not be vetoed by one: the provider has
    already rotated the grant, so discarding the new pair strands the connection on
    a refresh token that is dead upstream (a routine single-tenant 403 is enough to
    trigger it). Identity changes veto either way.
    """
    if not _identity_matches(preflight, current, connections):
        return False
    if (
        current.token != preflight.access_token
        or current.token_secret != preflight.refresh_token
        or current.expires_at != preflight.expires_at
    ):
        return False
    if not fence_denial:
        return True
    current_fences = {connection.id: _connection_fence(connection) for connection in connections}
    return all(
        current_fences[fence.connection_id].upstream_denied_at == fence.upstream_denied_at
        for fence in preflight.connection_fences
        if fence.connection_id in current_fences
    )


def _validate_refresh_response(response, preflight: _TokenPreflight) -> _ValidatedRefresh:
    try:
        data = response.json()
    except (TypeError, ValueError) as exc:
        raise TokenRefreshError("OAuth refresh returned an invalid response.") from exc
    if not isinstance(data, dict):
        raise TokenRefreshError("OAuth refresh returned an invalid response.")
    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise TokenRefreshError("OAuth refresh returned an invalid access token.")
    refresh_token = data.get("refresh_token") or preflight.refresh_token
    if not isinstance(refresh_token, str) or not refresh_token:
        raise TokenRefreshError("OAuth refresh returned an invalid refresh token.")
    expires_in = data.get("expires_in")
    if expires_in is None:
        expires_at = preflight.expires_at
    elif (
        isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or expires_in <= 0
    ):
        raise TokenRefreshError("OAuth refresh returned an invalid expiry.")
    else:
        expires_at = timezone.now() + timedelta(seconds=expires_in)
    return _ValidatedRefresh(access_token, refresh_token, expires_at)


def _preflight_snapshot(preflight: _TokenPreflight) -> PersistedTokenSnapshot:
    return PersistedTokenSnapshot(
        token_id=preflight.token_id,
        account_id=preflight.account_id,
        app_id=preflight.app_id,
        access_token=preflight.access_token,
        refresh_token=preflight.refresh_token,
        expires_at=preflight.expires_at,
    )


def _persisted_snapshot(token) -> PersistedTokenSnapshot:
    return PersistedTokenSnapshot(
        token_id=token.pk,
        account_id=token.account_id,
        app_id=token.app_id,
        access_token=token.token,
        refresh_token=token.token_secret,
        expires_at=token.expires_at,
    )


def _apply_persisted_snapshot(target, snapshot: PersistedTokenSnapshot) -> None:
    target.token = snapshot.access_token
    target.token_secret = snapshot.refresh_token
    target.expires_at = snapshot.expires_at
    target.app_id = snapshot.app_id
    target.account_id = snapshot.account_id


def _persist_refresh_response(
    preflight: _TokenPreflight,
    refreshed: _ValidatedRefresh,
    fingerprint: str,
    *,
    deadline=None,
    clock=time.monotonic,
) -> TokenRefreshResult:
    with transaction.atomic(), preserve_transaction_timeouts():
        try:
            current, connections = _lock_refresh_context(preflight, deadline=deadline, clock=clock)
        except (User.DoesNotExist, SocialToken.DoesNotExist):
            # Disconnected between preflight and persist: the very race the CAS is for.
            # There is no row left to mark, so report it rather than let the lookup
            # escape as an unclassified persistence failure.
            return TokenRefreshResult(
                TokenRefreshStatus.SUPERSEDED,
                _preflight_snapshot(preflight),
                credential_advanced=False,
            )
        _ensure_before_deadline(deadline, clock)
        if not _refresh_context_matches(preflight, current, connections, fence_denial=False):
            if not _identity_matches(preflight, current, connections):
                # The row no longer describes the binding the caller validated, so the
                # credential on it is not ours to hand back -- returning it would serve a
                # token from a principal the caller never saw, which is the exposure the
                # identity fences exist to stop. Report what preflight held so nothing
                # foreign crosses the boundary, and mark nothing: those connection rows
                # may belong to someone else now.
                return TokenRefreshResult(
                    TokenRefreshStatus.SUPERSEDED,
                    _preflight_snapshot(preflight),
                    credential_advanced=False,
                )
            snapshot = _persisted_snapshot(current)
            advanced = snapshot.access_token != preflight.access_token
            if not advanced:
                # Nobody else refreshed, so the credential we just rotated away is
                # what stays on the row. Mark it so token_health surfaces a reconnect
                # instead of reporting a connection that can never refresh again.
                _configure_transaction_deadline(deadline, clock)
                TenantConnection.objects.filter(
                    pk__in=[fence.connection_id for fence in preflight.connection_fences]
                ).update(oauth_refresh_failure_fingerprint=fingerprint)
            return TokenRefreshResult(
                TokenRefreshStatus.SUPERSEDED, snapshot, credential_advanced=advanced
            )
        current.token = refreshed.access_token
        current.token_secret = refreshed.refresh_token
        current.expires_at = refreshed.expires_at
        _configure_transaction_deadline(deadline, clock)
        current.save(update_fields=["token", "token_secret", "expires_at"])
        _configure_transaction_deadline(deadline, clock)
        TenantConnection.objects.filter(
            pk__in=[fence.connection_id for fence in preflight.connection_fences],
            oauth_refresh_failure_fingerprint=fingerprint,
        ).update(oauth_refresh_failure_fingerprint="")
        return TokenRefreshResult(TokenRefreshStatus.APPLIED, _persisted_snapshot(current))


async def _apersist_refresh_response(
    preflight: _TokenPreflight,
    refreshed: _ValidatedRefresh,
    fingerprint: str,
    *,
    deadline=None,
    clock=time.monotonic,
) -> TokenRefreshResult:
    return await sync_to_async(_persist_refresh_response)(
        preflight,
        refreshed,
        fingerprint,
        deadline=deadline,
        clock=clock,
    )


def _persist_refresh_failure(
    preflight: _TokenPreflight,
    fingerprint: str,
    *,
    deadline=None,
    clock=time.monotonic,
) -> bool:
    with transaction.atomic(), preserve_transaction_timeouts():
        try:
            current, connections = _lock_refresh_context(preflight, deadline=deadline, clock=clock)
        except (User.DoesNotExist, SocialToken.DoesNotExist):
            return False
        _ensure_before_deadline(deadline, clock)
        if not _refresh_context_matches(preflight, current, connections, fence_denial=True):
            return False
        _configure_transaction_deadline(deadline, clock)
        TenantConnection.objects.filter(
            pk__in=[fence.connection_id for fence in preflight.connection_fences]
        ).update(oauth_refresh_failure_fingerprint=fingerprint)
        return True


async def _apersist_refresh_failure(
    preflight: _TokenPreflight,
    fingerprint: str,
    *,
    deadline=None,
    clock=time.monotonic,
) -> bool:
    return await sync_to_async(_persist_refresh_failure)(
        preflight, fingerprint, deadline=deadline, clock=clock
    )


def _record_refresh_failure(
    preflight: _TokenPreflight, fingerprint: str, *, deadline=None, clock=time.monotonic
) -> None:
    try:
        _persist_refresh_failure(preflight, fingerprint, deadline=deadline, clock=clock)
    except (DatabaseError, _RefreshDeadlineExceeded) as exc:
        logger.warning("Failed to persist OAuth refresh failure", exc_info=True)
        raise TokenRefreshUnavailable("Failed to persist OAuth refresh failure.") from exc


async def _arecord_refresh_failure(
    preflight: _TokenPreflight, fingerprint: str, *, deadline=None, clock=time.monotonic
) -> None:
    try:
        await _apersist_refresh_failure(preflight, fingerprint, deadline=deadline, clock=clock)
    except (DatabaseError, _RefreshDeadlineExceeded) as exc:
        logger.warning("Failed to persist OAuth refresh failure", exc_info=True)
        raise TokenRefreshUnavailable("Failed to persist OAuth refresh failure.") from exc


async def refresh_oauth_token_result(
    social_token,
    token_url: str,
    *,
    request_timeout: float = 30,
    db_timeout: float | None = WORKER_DB_DEADLINE,
    deadline=None,
    clock=time.monotonic,
) -> TokenRefreshResult:
    """Refresh an OAuth token using the refresh token grant.

    Args:
        social_token: allauth SocialToken instance with token_secret (refresh token)
            and app (SocialApp with client_id and secret).
        token_url: The provider's token endpoint URL.

    Returns:
        The persisted token snapshot and whether this response won the CAS.

    Raises:
        TokenRefreshError: If the refresh request fails.
    """
    try:
        preflight = await sync_to_async(_preflight_token)(
            social_token, deadline=_phase_deadline(deadline, db_timeout, clock), clock=clock
        )
    except (DatabaseError, _RefreshDeadlineExceeded) as exc:
        if _is_deadline_error(exc):
            raise TokenRefreshDeadlineExceeded(
                "Timed out waiting on the database before refreshing OAuth credentials."
            ) from exc
        raise TokenRefreshUnavailable("Failed to read OAuth refresh state.") from exc
    fingerprint = credential_fingerprint(social_token)
    if social_token.app is None:
        raise TokenRefreshError("OAuth application is missing; reconnect this account.")
    try:
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            response = await client.post(
                token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": social_token.token_secret,
                    "client_id": social_token.app.client_id,
                    "client_secret": social_token.app.secret,
                },
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as e:
        # A 4xx (typically 400 invalid_grant on a dead refresh token) is an
        # expected outcome, not a bug. Keep provider response bodies out of logs.
        rejected = _is_invalid_grant(e.response)
        misconfigured = _is_invalid_client(e.response)
        if 400 <= e.response.status_code < 500:
            logger.warning(
                "Token refresh rejected for app %s: HTTP %s (%s)",
                social_token.app.client_id,
                e.response.status_code,
                "invalid_grant" if rejected else "invalid_client" if misconfigured else "other",
            )
        else:
            logger.exception("Token refresh failed for app %s", social_token.app.client_id)
        try:
            # Deliberately not the caller's deadline: it may already be exhausted, and
            # starving the marker is how a diagnosable failure becomes a silent one.
            await _arecord_refresh_failure(
                preflight,
                fingerprint,
                deadline=_phase_deadline(None, db_timeout, clock),
                clock=clock,
            )
        except TokenRefreshUnavailable:
            logger.warning("Could not record OAuth refresh failure marker", exc_info=True)
        if rejected:
            raise TokenRefreshRejected("OAuth refresh grant was rejected as invalid.") from e
        if e.response.status_code in (408, 429) or e.response.status_code >= 500:
            raise TokenRefreshUnavailable(f"Failed to refresh OAuth token: {e}") from e
        raise TokenRefreshError(f"Failed to refresh OAuth token: {e}") from e
    except Exception as e:
        logger.exception("Token refresh failed for app %s", social_token.app.client_id)
        try:
            await _arecord_refresh_failure(
                preflight,
                fingerprint,
                deadline=_phase_deadline(None, db_timeout, clock),
                clock=clock,
            )
        except TokenRefreshUnavailable:
            # Losing the marker must not erase the real cause; it is already logged.
            logger.warning("Could not record OAuth refresh failure marker", exc_info=True)
        if isinstance(e, (httpx.RequestError, requests.RequestException)):
            raise TokenRefreshUnavailable(f"Failed to refresh OAuth token: {e}") from e
        raise TokenRefreshError(f"Failed to refresh OAuth token: {e}") from e

    refreshed = _validate_refresh_response(response, preflight)
    try:
        result = await _apersist_refresh_response(
            preflight,
            refreshed,
            fingerprint,
            deadline=_phase_deadline(deadline, db_timeout, clock),
            clock=clock,
        )
    except (DatabaseError, _RefreshDeadlineExceeded) as exc:
        if _is_deadline_error(exc) and _grant_was_spent(preflight, refreshed):
            logger.warning("Timed out persisting a rotated OAuth token", exc_info=True)
            raise TokenRefreshRejected(
                "The refreshed OAuth credential could not be stored in time; reconnect required."
            ) from exc
        logger.warning("Failed to persist refreshed OAuth token", exc_info=True)
        if _is_deadline_error(exc):
            raise TokenRefreshDeadlineExceeded(
                "Timed out storing refreshed OAuth credentials."
            ) from exc
        raise TokenRefreshUnavailable("Failed to persist refreshed OAuth token.") from exc
    _apply_persisted_snapshot(social_token, result.snapshot)
    if result.status is TokenRefreshStatus.SUPERSEDED:
        logger.warning(
            "Lost OAuth refresh race for app %s; stored credential %s",
            social_token.app.client_id,
            "was refreshed concurrently" if result.credential_advanced else "did not advance",
        )
    else:
        logger.info("Successfully refreshed OAuth token for app %s", social_token.app.client_id)
    return result


def _ensure_usable_credential(result: TokenRefreshResult) -> None:
    """Refuse to hand back a credential this refresh already rotated away upstream.

    Reconnect rather than retry: the stored refresh token is dead at the provider, so
    no amount of retrying will revive it.
    """
    if result.status is TokenRefreshStatus.SUPERSEDED and not result.credential_advanced:
        raise TokenRefreshRejected(
            "OAuth refresh was superseded; the stored credential can no longer be refreshed."
        )


async def refresh_oauth_token(
    social_token,
    token_url: str,
    *,
    request_timeout: float = 30,
    db_timeout: float | None = WORKER_DB_DEADLINE,
) -> str:
    result = await refresh_oauth_token_result(
        social_token, token_url, request_timeout=request_timeout, db_timeout=db_timeout
    )
    _ensure_usable_credential(result)
    return result.snapshot.access_token


def refresh_oauth_token_result_sync(
    social_token,
    token_url: str,
    *,
    timeout: float = 30,
    db_timeout: float | None = WORKER_DB_DEADLINE,
    deadline=None,
    clock=time.monotonic,
) -> TokenRefreshResult:
    """Blocking OAuth refresh for the materialization worker thread.

    Mirrors ``refresh_oauth_token`` but uses ``requests`` + sync ORM so a loader
    running under ``asyncio.to_thread`` can renew a token mid-run (arch #252,
    finding 14#3). The rotated token must persist before any caller may use it.
    """
    try:
        preflight = _preflight_token(
            social_token, deadline=_phase_deadline(deadline, db_timeout, clock), clock=clock
        )
    except (DatabaseError, _RefreshDeadlineExceeded) as exc:
        if _is_deadline_error(exc):
            raise TokenRefreshDeadlineExceeded(
                "Timed out waiting on the database before refreshing OAuth credentials."
            ) from exc
        raise TokenRefreshUnavailable("Failed to read OAuth refresh state.") from exc
    fingerprint = credential_fingerprint(social_token)
    if social_token.app is None:
        raise TokenRefreshError("OAuth application is missing; reconnect this account.")
    try:
        response = requests.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": social_token.token_secret,
                "client_id": social_token.app.client_id,
                "client_secret": social_token.app.secret,
            },
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.HTTPError as e:
        # Mirrors the async twin above: a 4xx (typically 400 invalid_grant on a
        # dead refresh token) is an expected outcome, not a bug. Keep provider
        # response bodies out of logs. The sync path never got this treatment,
        # so every routine dead-token refresh raised a Sentry event (#373).
        status = e.response.status_code if e.response is not None else None
        rejected = _is_invalid_grant(e.response)
        misconfigured = _is_invalid_client(e.response)
        if status is not None and 400 <= status < 500:
            logger.warning(
                "Sync token refresh rejected for app %s: HTTP %s (%s)",
                social_token.app.client_id,
                status,
                "invalid_grant" if rejected else "invalid_client" if misconfigured else "other",
            )
        else:
            logger.exception("Sync token refresh failed for app %s", social_token.app.client_id)
        try:
            _record_refresh_failure(
                preflight,
                fingerprint,
                deadline=_phase_deadline(None, db_timeout, clock),
                clock=clock,
            )
        except TokenRefreshUnavailable:
            logger.warning("Could not record OAuth refresh failure marker", exc_info=True)
        if rejected:
            raise TokenRefreshRejected("OAuth refresh grant was rejected as invalid.") from e
        if status is not None and (status in (408, 429) or status >= 500):
            raise TokenRefreshUnavailable(f"Failed to refresh OAuth token: {e}") from e
        raise TokenRefreshError(f"Failed to refresh OAuth token: {e}") from e
    except Exception as e:
        logger.exception("Sync token refresh failed for app %s", social_token.app.client_id)
        try:
            _record_refresh_failure(
                preflight,
                fingerprint,
                deadline=_phase_deadline(None, db_timeout, clock),
                clock=clock,
            )
        except TokenRefreshUnavailable:
            # Losing the marker must not erase the real cause; it is already logged.
            logger.warning("Could not record OAuth refresh failure marker", exc_info=True)
        if isinstance(e, (httpx.RequestError, requests.RequestException)):
            raise TokenRefreshUnavailable(f"Failed to refresh OAuth token: {e}") from e
        raise TokenRefreshError(f"Failed to refresh OAuth token: {e}") from e

    refreshed = _validate_refresh_response(response, preflight)
    try:
        result = _persist_refresh_response(
            preflight,
            refreshed,
            fingerprint,
            deadline=_phase_deadline(deadline, db_timeout, clock),
            clock=clock,
        )
    except (DatabaseError, _RefreshDeadlineExceeded) as exc:
        if _is_deadline_error(exc) and _grant_was_spent(preflight, refreshed):
            logger.warning("Timed out persisting a rotated OAuth token", exc_info=True)
            raise TokenRefreshRejected(
                "The refreshed OAuth credential could not be stored in time; reconnect required."
            ) from exc
        logger.warning("Failed to persist refreshed OAuth token", exc_info=True)
        if _is_deadline_error(exc):
            raise TokenRefreshDeadlineExceeded(
                "Timed out storing refreshed OAuth credentials."
            ) from exc
        raise TokenRefreshUnavailable("Failed to persist refreshed OAuth token.") from exc
    _apply_persisted_snapshot(social_token, result.snapshot)
    if result.status is TokenRefreshStatus.SUPERSEDED:
        logger.warning(
            "Lost OAuth refresh race (sync) for app %s; stored credential %s",
            social_token.app.client_id,
            "was refreshed concurrently" if result.credential_advanced else "did not advance",
        )
    else:
        logger.info(
            "Successfully refreshed OAuth token (sync) for app %s", social_token.app.client_id
        )
    return result


def refresh_oauth_token_sync(
    social_token,
    token_url: str,
    *,
    timeout: float = 30,
    db_timeout: float | None = WORKER_DB_DEADLINE,
) -> str:
    result = refresh_oauth_token_result_sync(
        social_token, token_url, timeout=timeout, db_timeout=db_timeout
    )
    _ensure_usable_credential(result)
    return result.snapshot.access_token
