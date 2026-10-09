"""
Tenant resolution for OAuth providers.

After a user authenticates (or a server-side refresh runs on their behalf), this
service queries the provider's API to discover which tenants (domains / opps /
chatbots) the user currently belongs to, and **full-syncs** the corresponding
TenantMembership records: new access is added/un-archived, and access the provider
no longer returns is archived (revoked).

Revocation safety — a fetch that silently returned a *partial* or *shape-drifted*
result would wrongly archive access the user still has, so every fetch either
returns a provably complete set or raises. Only authoritative denial triggers
archival on an unsuccessful fetch:
  * HTTP 401/403 → record denial for the observed connection, then raise.
    Connect org/program export-list 403 is an endpoint-specific exception:
    it can reject export scope without revoking opportunity membership.
    CommCare user-domain and OCS team-membership lists retain scoped denial semantics.
  * Other non-2xx → raise without revoking access.
  * missing expected key in a 2xx body → raise (never treat drift as "zero tenants").
    A per-row display name is not such a key: a missing one falls back to the
    external id in ``Tenant.save``.
  * CommCare pagination that can't be followed → raise (no silent truncation).
Callers (the login signal and ``tenant_list_view``) treat any raise as "skip
refresh," so access is never revoked on an inconclusive fetch (fail-open).

A discovery the user did not start (``may_revoke=False``: a manager's add replaying
members' stored tokens) records no denial and archives nothing. Only the user's own
sign-in may take their access away (#561 G1).
"""

from __future__ import annotations

import logging

import httpx
from allauth.socialaccount.models import SocialToken
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.common.commcare_servers import CommCareServer, get_commcare_server
from apps.common.error_codes import ErrorCode
from apps.common.errors import CommCareAuthError, ConnectAuthError, OCSAuthError
from apps.users.models import Tenant, TenantConnection, TenantMembership, User
from apps.users.services.oauth_scope import (
    account_scope,
    ocs_scope_unusable,
    provider_accounts,
    scope_account_ids,
)
from apps.users.services.ocs_team import adetect_team_name_from_oauth
from apps.users.services.tenant_listing import commcare as commcare_listing
from apps.users.services.tenant_listing import connect as connect_listing
from apps.users.services.tenant_listing import ocs as ocs_listing
from apps.users.services.tenant_listing.paginator import list_tenants
from apps.users.services.tenant_listing.types import (
    MalformedTenantList,
    ProviderRequest,
    TenantDescriptor,
    TenantListError,
    UnsafeListingOrigin,
    UnsafeNextURL,
    UpstreamStatus,
    UpstreamUnreachable,
)
from apps.users.services.upstream_denial import (
    adiscovery_connection,
    arecord_upstream_denial,
    credential_is_current,
)
from apps.workspaces import access_cache

logger = logging.getLogger(__name__)

_MAX_PAGES = 100
_LISTING_BUDGET_SECONDS = 60.0
_REQUEST_TIMEOUT_SECONDS = 30.0


async def _anewest_account(user, provider: str):
    """The user's most recently authorised identity for *provider*.

    Only a fallback for callers that did not say which identity they are
    resolving. Ordered, because a user can hold one identity per team and an
    unordered read would attribute a fetch to an arbitrary one of them.
    """
    return await provider_accounts(user.pk, provider).order_by("-date_joined", "-id").afirst()


@sync_to_async
def _aoauth_connection(
    user,
    provider: str,
    *,
    scope_key: str,
    scope_label: str,
    account,
    allow_replace=True,
    observed_connection=None,
    access_token=None,
    observation_taken=False,
):
    """Bind a validated identity and retire superseded credentials for this scope.

    Background fetches may finish after reconnect/disconnect. They must not
    replace the current binding or recreate a connection from a retired token.
    SocialAccount rows remain available for a later deliberate OAuth login.
    """
    with transaction.atomic():
        # Serialize scope creation/replacement against disconnect, including absent rows.
        User.objects.select_for_update().get(pk=user.pk)
        conn = TenantConnection.objects.filter(
            user=user,
            provider=provider,
            credential_type=TenantConnection.OAUTH,
            scope_key=scope_key,
        ).first()
        if observation_taken and observed_connection is None and conn is not None:
            logger.info(
                "Skipping discovery: connection appeared for user=%s provider=%s", user.pk, provider
            )
            return None
        if observed_connection is not None:
            if (
                conn is None
                or conn.pk != observed_connection.pk
                or conn.social_account_id != observed_connection.social_account_id
            ):
                logger.info(
                    "Skipping discovery: connection changed for user=%s provider=%s",
                    user.pk,
                    provider,
                )
                return None
            if conn.upstream_denied_at != observed_connection.upstream_denied_at:
                logger.info("Skipping discovery: newer denial for connection=%s", conn.pk)
                return None
        if access_token is not None:
            if account is not None:
                current_token = bool(access_token) and any(
                    token.token == access_token
                    for token in SocialToken.objects.select_for_update()
                    .filter(account=account)
                    .order_by("pk")
                )
            else:
                current_token = not (conn and conn.social_account_id) or credential_is_current(
                    conn, access_token
                )
            if not current_token:
                logger.info(
                    "Skipping discovery: credential changed for user=%s provider=%s",
                    user.pk,
                    provider,
                )
                return None
        if not allow_replace and account is not None:
            if not SocialToken.objects.filter(account=account).exists():
                return None
            if conn and conn.social_account_id not in (None, account.pk):
                return None
        defaults = {}
        if account is not None:
            defaults["social_account"] = account
        if scope_label:
            defaults["scope_label"] = scope_label
        conn, _ = TenantConnection.objects.update_or_create(
            user=user,
            provider=provider,
            credential_type=TenantConnection.OAUTH,
            scope_key=scope_key,
            defaults=defaults,
        )
        if account is not None:
            SocialToken.objects.filter(
                account_id__in=scope_account_ids(user.pk, provider, scope_key)
            ).exclude(account=account).delete()
        return conn


class TenantResolutionError(Exception):
    """Raised when a provider returns a 2xx body of an unexpected shape.

    Treated like an auth error by callers (skip refresh) — the point is to abort
    before archival rather than mistake shape drift for "user has zero tenants."
    """


@sync_to_async
def _sync_memberships(
    user,
    connection: TenantConnection,
    fresh_tenants: list[Tenant],
    *,
    membership_extra: dict | None = None,
    archive_team_slug: str | None = None,
    observed_connection=None,
    access_token=None,
    archive=True,
) -> list[TenantMembership]:
    """Upsert memberships for ``fresh_tenants`` and archive this connection's stale ones.

    ``fresh_tenants`` must be the **complete** set the upstream fetch returned.
    A discovery begun after denial restores listed tenant memberships, including
    earlier tenant-403 tombstones. This is the membership recovery contract; it
    does not prove every loader/export endpoint is usable. Persisting endpoint
    denial through discovery would also need a separate recovery probe/lifecycle.
    Uses ``all_objects`` so a revoked tombstone is reused (un-archived) instead of
    colliding on ``unique(user, tenant)``. Archival is scoped to ``connection`` — so
    an OAuth refresh never touches an API-key connection's memberships or another
    provider's — and additionally to ``archive_team_slug`` for OCS, whose tokens are
    team-scoped (a successful fetch only covers the current team; other teams' access
    must be left intact). If a team-scoped provider has no resolvable team slug,
    archival is skipped entirely (additive only) since it can't be scoped safely.
    ``archive=False`` is additive only too, for a discovery the user did not start.
    """
    with transaction.atomic():
        # A reconnect/disconnect must not interleave between this check and archival.
        User.objects.select_for_update().get(pk=user.pk)
        if not TenantConnection.objects.filter(
            pk=connection.pk, user=user, social_account_id=connection.social_account_id
        ).exists():
            logger.info("Skipping membership sync: connection changed id=%s", connection.pk)
            return []
        current = TenantConnection.objects.get(pk=connection.pk)
        observation = observed_connection or connection
        if current.upstream_denied_at != observation.upstream_denied_at:
            logger.info("Skipping membership sync: newer denial for connection=%s", connection.pk)
            return []
        if (
            access_token is not None
            and current.social_account_id
            and not credential_is_current(current, access_token)
        ):
            logger.info(
                "Skipping membership sync: credential changed for connection=%s", connection.pk
            )
            return []
        # Keep the last denial as a fence against discoveries started before it.
        if current.upstream_denial_code:
            TenantConnection.objects.filter(pk=connection.pk).update(upstream_denial_code="")
        fresh_ids: set = set()
        memberships: list[TenantMembership] = []
        for tenant in fresh_tenants:
            tm, _ = TenantMembership.all_objects.get_or_create(user=user, tenant=tenant)
            tm.connection = connection
            tm.archived_at = None
            tm.archived_reason = ""
            fields = ["connection", "archived_at", "archived_reason"]
            if membership_extra:
                for attr, val in membership_extra.items():
                    setattr(tm, attr, val)  # team_slug/team_name setters mutate provider_metadata
                fields.append("provider_metadata")
            tm.save(update_fields=fields)
            memberships.append(tm)
            fresh_ids.add(tenant.id)
        # Rediscovery can run inside a workspace-gated request (member refresh on
        # source add); coverage it changes must not be served from that request's cache.
        user_id = user.pk
        transaction.on_commit(lambda: access_cache.invalidate(user_id=user_id))

        if not archive:
            return memberships
        unlisted_qs = TenantMembership.all_objects.filter(user=user, connection=connection).exclude(
            tenant_id__in=fresh_ids
        )
        if archive_team_slug is not None:
            if not archive_team_slug:
                return memberships  # team-scoped provider without a team → never revoke
            unlisted_qs = unlisted_qs.filter(provider_metadata__team_slug=archive_team_slug)
        unlisted_qs.filter(archived_at__isnull=True).update(
            archived_at=timezone.now(), archived_reason=TenantMembership.ARCHIVED_UNLISTED
        )
        # The listing works, so a source the connection-wide denial archived that it
        # still omits is gone. A per-source denial stays: omission is how the
        # provider withholds a source it denied.
        unlisted_qs.filter(archived_reason=TenantMembership.ARCHIVED_DENIED_CONNECTION).update(
            archived_reason=TenantMembership.ARCHIVED_UNLISTED
        )
        return memberships


async def _aaccount_holding(user, provider: str, access_token: str):
    """The identity whose stored token is ``access_token``, if any."""
    async for token in SocialToken.objects.filter(
        account__in=provider_accounts(user.pk, provider)
    ).select_related("account"):
        if access_token and token.token == access_token:
            return token.account
    return None


async def resolve_commcare_domains(
    user, access_token: str, *, social_account=None, allow_replace=True, may_revoke=True
) -> list[TenantMembership]:
    """Fetch the user's CommCare domains and full-sync TenantMembership records.

    A CommCare HQ token is account-wide on the server that issued it, so there is
    one connection per user per server (``scope_key`` is the server key, ``""`` for
    www); ``social_account`` pins which identity holds it and so which server to ask.
    """
    # The identity decides the server, so a caller that did not name it gets the one
    # holding this token, bound as if named: an unbound EU connection could otherwise
    # fall back to a www token.
    account = social_account or await _aaccount_holding(user, "commcare", access_token)
    server = get_commcare_server(account_scope(account))
    observed = await adiscovery_connection(user, "commcare", access_token, account)
    try:
        domains = await _fetch_all_domains(access_token, server)
    except CommCareAuthError as error:
        if may_revoke:
            await _record_discovery_denial(observed, access_token, error.status_code, account)
        raise
    conn = await _aoauth_connection(
        user,
        "commcare",
        scope_key=server.key,
        scope_label=server.label if server.key else "",
        account=account,
        allow_replace=allow_replace,
        observed_connection=observed,
        access_token=access_token,
        observation_taken=True,
    )
    if conn is None:
        return []
    fresh = []
    for domain in domains:
        tenant, _ = await Tenant.objects.aupdate_or_create(
            provider="commcare",
            server=server.key,
            external_id=domain.external_id,
            defaults={"canonical_name": domain.canonical_name},
        )
        fresh.append(tenant)

    memberships = await _sync_memberships(
        user,
        conn,
        fresh,
        observed_connection=observed,
        access_token=access_token,
        archive=may_revoke,
    )
    logger.info("Resolved %d CommCare domains for user %s", len(memberships), user.email)
    return memberships


async def resolve_connect_opportunities(
    user, access_token: str, *, social_account=None, allow_replace=True, may_revoke=True
) -> list[TenantMembership]:
    """Fetch the user's Connect opportunities and full-sync TenantMembership records.

    A Connect token is account-wide, so this stays one connection per user
    (``scope_key=""``); ``social_account`` only pins which identity holds it.
    """
    observed = await adiscovery_connection(user, "commcare_connect", access_token, social_account)
    try:
        opportunities = await _fetch_connect_opportunities(access_token)
    except ConnectAuthError as error:
        # Export-list permission can be denied while opportunity membership remains valid.
        if error.status_code == 401 and may_revoke:
            await _record_discovery_denial(
                observed, access_token, error.status_code, social_account
            )
        raise

    conn = await _aoauth_connection(
        user,
        "commcare_connect",
        scope_key="",
        scope_label="",
        account=social_account,
        allow_replace=allow_replace,
        observed_connection=observed,
        access_token=access_token,
        observation_taken=True,
    )
    if conn is None:
        return []
    fresh = []
    for opp in opportunities:
        tenant, _ = await Tenant.objects.aupdate_or_create(
            provider="commcare_connect",
            external_id=opp.external_id,
            defaults={
                "canonical_name": opp.canonical_name,
                "provider_attributes": dict(opp.attributes or {}),
            },
        )
        fresh.append(tenant)

    memberships = await _sync_memberships(
        user,
        conn,
        fresh,
        observed_connection=observed,
        access_token=access_token,
        archive=may_revoke,
    )
    logger.info("Resolved %d Connect opportunities for user %s", len(memberships), user.email)
    return memberships


async def resolve_ocs_chatbots(
    user, access_token: str, *, social_account=None, allow_replace=True, may_revoke=True
) -> list[TenantMembership]:
    """Fetch the user's OCS chatbots (experiments) and full-sync TenantMembership records.

    OCS tokens are **team-scoped** — a successful ``/api/experiments/`` fetch returns
    only the team the user selected during OAuth consent. Archival is therefore
    restricted to that team's memberships (``archive_team_slug``); memberships from
    other teams the user previously authorized are left untouched. Under all-of
    access an identity with no team discovers nothing; the user must reconnect
    choosing a team.
    """
    base_url = getattr(settings, "OCS_URL", "https://www.openchatstudio.com").rstrip("/")

    account = social_account if social_account is not None else await _anewest_account(user, "ocs")
    team_slug = account_scope(account)
    if ocs_scope_unusable("ocs", team_slug):
        # A settled outcome, not drift: raising would read as "retry may help" to
        # every caller and loop on each refresh.
        logger.info(
            "Skipping OCS discovery: identity %s carries no team",
            account.pk if account else None,
        )
        return []
    observed = await adiscovery_connection(user, "ocs", access_token, account)
    try:
        experiments = await _fetch_ocs_experiments(access_token, base_url)
    except OCSAuthError as error:
        if may_revoke:
            await _record_discovery_denial(observed, access_token, error.status_code, account)
        raise
    # After the listing, whose origin check guards OCS_URL: the team lookup has none.
    team_name = (await adetect_team_name_from_oauth(access_token, base_url)) or team_slug

    conn = await _aoauth_connection(
        user,
        "ocs",
        scope_key=team_slug,
        scope_label=team_name,
        account=account,
        allow_replace=allow_replace,
        observed_connection=observed,
        access_token=access_token,
        observation_taken=True,
    )

    if conn is None:
        return []

    fresh = []
    for exp in experiments:
        tenant, _ = await Tenant.objects.aupdate_or_create(
            provider="ocs",
            external_id=exp.external_id,
            defaults={"canonical_name": exp.canonical_name},
        )
        fresh.append(tenant)

    memberships = await _sync_memberships(
        user,
        conn,
        fresh,
        membership_extra={"team_slug": team_slug, "team_name": team_name},
        archive_team_slug=team_slug,
        observed_connection=observed,
        access_token=access_token,
        archive=may_revoke,
    )
    logger.info(
        "Resolved %d OCS chatbots for user %s (team %s)", len(memberships), user.email, team_slug
    )
    return memberships


async def _record_discovery_denial(connection, access_token, status, account=None):
    if (
        account is not None
        and connection is not None
        and connection.social_account_id not in (None, account.pk)
    ):
        logger.warning(
            "Skipping discovery denial: identity changed for connection=%s", connection.pk
        )
        return
    if status not in (401, 403):
        logger.warning(
            "Skipping discovery denial: unsupported status for connection=%s",
            connection.pk if connection else None,
        )
        return
    await arecord_upstream_denial(
        connection,
        credential=access_token,
        code=ErrorCode.AUTH_TOKEN_EXPIRED if status == 401 else ErrorCode.AUTH_ACCESS_DENIED,
    )


async def _fetch_tenant_list(
    label: str,
    noun: str,
    request: ProviderRequest,
    decode_page,
    auth_error: type[Exception],
    *,
    grant: str = "API",
    setting: str = "",
    max_pages: int = _MAX_PAGES,
) -> list[TenantDescriptor]:
    """Every tenant the credential can see, or a raise; never a partial list.

    Raises ``auth_error`` on 401/403, httpx.HTTPStatusError on any other status,
    the httpx error on a transport failure, and TenantResolutionError when the list
    cannot be read to its end (shape drift, a next link off the origin, a cycle, the
    page or time limit, or a request cut off at the budget).
    """
    try:
        async with httpx.AsyncClient() as client:
            return await list_tenants(
                client,
                request,
                decode_page,
                budget_seconds=_LISTING_BUDGET_SECONDS,
                max_pages=max_pages,
                request_timeout=_REQUEST_TIMEOUT_SECONDS,
            )
    except UpstreamStatus as error:
        if error.status_code in (401, 403):
            raise auth_error(
                f"{label} returned {error.status_code} while listing {noun} — the "
                f"access token is expired, revoked, or not authorized for this {grant}",
                status_code=error.status_code,
            ) from None
        raise httpx.HTTPStatusError(
            f"{label} answered HTTP {error.status_code} while listing {noun}",
            request=error.response.request,
            response=error.response,
        ) from None
    except UpstreamUnreachable as error:
        raise error.cause from None
    except UnsafeListingOrigin as error:
        raise TenantResolutionError(
            f"{setting or label} is not a safe provider origin: {error}"
        ) from None
    except UnsafeNextURL:
        # Following it would send the token to another origin.
        raise TenantResolutionError(f"{label} pagination left its server") from None
    except MalformedTenantList as error:
        raise TenantResolutionError(f"{label} returned an unexpected list: {error}") from None
    except TenantListError as error:
        raise TenantResolutionError(
            f"{label} list did not finish ({type(error).__name__})"
        ) from None


async def _fetch_all_domains(access_token: str, server: CommCareServer) -> list[TenantDescriptor]:
    request = commcare_listing.list_request(server.key, TenantConnection.OAUTH, access_token)
    if request is None:
        raise TenantResolutionError(f"No CommCare domain list for server {server.key!r}")
    return await _fetch_tenant_list(
        "CommCare", "domains", request, commcare_listing.decode_page, CommCareAuthError
    )


async def _fetch_ocs_experiments(access_token: str, base_url: str) -> list[TenantDescriptor]:
    request = ocs_listing.list_request(base_url, TenantConnection.OAUTH, access_token)
    return await _fetch_tenant_list(
        "OCS",
        "experiments",
        request,
        ocs_listing.decode_page,
        OCSAuthError,
        grant="team",
        setting="OCS_URL",
    )


async def _fetch_connect_opportunities(access_token: str) -> list[TenantDescriptor]:
    base_url = getattr(settings, "CONNECT_API_URL", "https://connect.dimagi.com")
    request = connect_listing.list_request(base_url, TenantConnection.OAUTH, access_token)
    # One unpaginated export; the decoder rejects any page that says otherwise.
    return await _fetch_tenant_list(
        "Connect",
        "opportunities",
        request,
        connect_listing.decode_page,
        ConnectAuthError,
        setting="CONNECT_API_URL",
        max_pages=1,
    )
