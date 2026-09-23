"""Read-only local credential readiness for workspace members.

This service does not make provider requests and does not prove that an upstream
credential is still accepted. It checks whether Scout has enough internally
consistent local state to attempt every tenant in a workspace. Authorization
remains a separate decision in ``apps.workspaces.access``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import StrEnum

from allauth.socialaccount.models import SocialToken

from apps.users.adapters import decrypt_credential
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.oauth_scope import (
    account_scope,
    canonical_provider,
    is_active_identity,
    oauth_membership_scope_mismatch,
    same_provider,
)
from apps.users.services.token_refresh import (
    credential_fingerprint,
    get_token_url,
    token_health,
    token_needs_refresh,
)
from apps.workspaces.models import WorkspaceMembership, WorkspaceTenant

LOCAL_CREDENTIAL_READINESS = "local_credential_readiness"


class CredentialGapCode(StrEnum):
    MISSING_LIVE_MEMBERSHIP = "missing_live_membership"
    MISSING_CONNECTION = "missing_connection"
    CONNECTION_USER_MISMATCH = "connection_user_mismatch"
    CONNECTION_PROVIDER_MISMATCH = "connection_provider_mismatch"
    CREDENTIAL_TYPE_UNSUPPORTED = "credential_type_unsupported"
    API_KEY_MISSING = "api_key_missing"
    API_KEY_DECRYPT_FAILED = "api_key_decrypt_failed"
    OCS_API_KEY_TEAM_AMBIGUOUS = "ocs_api_key_team_ambiguous"
    OAUTH_ACCOUNT_MISSING = "oauth_account_missing"
    OAUTH_ACCOUNT_USER_MISMATCH = "oauth_account_user_mismatch"
    OAUTH_ACCOUNT_PROVIDER_MISMATCH = "oauth_account_provider_mismatch"
    OAUTH_CONNECTION_INACTIVE = "oauth_connection_inactive"
    OAUTH_CREDENTIAL_MISSING = "oauth_token_missing"
    OAUTH_CREDENTIAL_EMPTY = "oauth_token_empty"
    OAUTH_CREDENTIAL_EXPIRED = "oauth_token_expired"
    OAUTH_REFRESH_FAILED = "oauth_refresh_failed"
    OAUTH_SCOPE_MISMATCH = "oauth_scope_mismatch"
    OCS_TEAM_MISSING = "ocs_team_missing"
    OCS_CONNECTION_SCOPE_MISSING = "ocs_connection_scope_missing"
    OCS_CONNECTION_SCOPE_MISMATCH = "ocs_connection_scope_mismatch"
    OCS_ACCOUNT_SCOPE_MISSING = "ocs_account_scope_missing"
    OCS_ACCOUNT_SCOPE_MISMATCH = "ocs_account_scope_mismatch"


@dataclass(frozen=True)
class CredentialCoverageGap:
    code: CredentialGapCode
    tenant_id: str
    tenant_external_id: str
    tenant_name: str
    provider: str
    membership_id: str | None = None
    team_slug: str = ""
    team_name: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class WorkspaceCredentialCoverage:
    workspace_id: str
    workspace_name: str
    user_id: int
    covered: bool
    gaps: tuple[CredentialCoverageGap, ...]
    readiness: str = LOCAL_CREDENTIAL_READINESS

    def as_dict(self) -> dict:
        value = asdict(self)
        value["gaps"] = [gap.as_dict() for gap in self.gaps]
        return value


@dataclass(frozen=True)
class TenantCredentialReadiness:
    """Local readiness for one explicit user/tenant pair."""

    user_id: int
    tenant_id: str
    usable: bool
    gap: CredentialCoverageGap | None
    readiness: str = LOCAL_CREDENTIAL_READINESS


def _workspace_membership_queryset(*, workspace_ids=None, user_ids=None):
    queryset = WorkspaceMembership.objects.select_related("workspace").order_by(
        "workspace__name", "workspace_id", "user_id"
    )
    if workspace_ids is not None:
        queryset = queryset.filter(workspace_id__in=workspace_ids)
    if user_ids is not None:
        queryset = queryset.filter(user_id__in=user_ids)
    return queryset


def _workspace_tenant_queryset(workspace_ids):
    return (
        WorkspaceTenant.objects.filter(workspace_id__in=workspace_ids)
        .select_related("tenant")
        .order_by("workspace_id", "tenant__canonical_name", "tenant_id")
    )


def _tenant_membership_queryset(user_ids, tenant_ids):
    return TenantMembership.objects.filter(
        user_id__in=user_ids,
        tenant_id__in=tenant_ids,
    ).select_related("tenant", "connection", "connection__social_account")


def _oauth_connection_queryset(user_ids):
    return TenantConnection.objects.filter(
        user_id__in=user_ids,
        credential_type=TenantConnection.OAUTH,
    )


def _token_queryset(account_ids):
    # aget_connection_token() resolves the lowest-pk token via afirst(). Audits
    # must choose the same row or they can claim a connection is usable when the
    # runtime resolver will pick an older, dead token.
    return (
        SocialToken.objects.filter(account_id__in=account_ids)
        .select_related("account", "app")
        .order_by("pk")
    )


def _connection_membership_queryset(connection_ids):
    return TenantMembership.objects.filter(connection_id__in=connection_ids).only(
        "connection_id", "provider_metadata"
    )


def _bindings_by_user(connections) -> dict[int, dict[tuple[str, str], int | None]]:
    result: dict[int, dict[tuple[str, str], int | None]] = defaultdict(dict)
    for connection in connections:
        if connection.social_account_id is None:
            continue
        key = (canonical_provider(connection.provider), connection.scope_key)
        bindings = result[connection.user_id]
        bound_id = bindings.get(key)
        if key in bindings and bound_id != connection.social_account_id:
            bindings[key] = -1
        else:
            bindings[key] = connection.social_account_id
    return result


def _normalized_team_slug(membership) -> str:
    return str(membership.team_slug or "").strip()


def _normalized_team_name(membership) -> str:
    return str(membership.team_name or "").strip()


def _gap(code: CredentialGapCode, tenant, membership=None) -> CredentialCoverageGap:
    return CredentialCoverageGap(
        code=code,
        tenant_id=str(tenant.id),
        tenant_external_id=tenant.external_id,
        tenant_name=tenant.canonical_name,
        provider=tenant.provider,
        membership_id=str(membership.id) if membership else None,
        team_slug=_normalized_team_slug(membership) if membership else "",
        team_name=_normalized_team_name(membership) if membership else "",
    )


def _api_key_gap(membership, connection, connection_teams):
    if canonical_provider(connection.provider) == "ocs" and connection_teams.get(
        connection.pk, set()
    ) != {_normalized_team_slug(membership)}:
        return _gap(CredentialGapCode.OCS_API_KEY_TEAM_AMBIGUOUS, membership.tenant, membership)
    if not connection.encrypted_credential:
        return _gap(CredentialGapCode.API_KEY_MISSING, membership.tenant, membership)
    try:
        value = decrypt_credential(connection.encrypted_credential)
    except Exception:
        return _gap(CredentialGapCode.API_KEY_DECRYPT_FAILED, membership.tenant, membership)
    if not value:
        return _gap(CredentialGapCode.API_KEY_MISSING, membership.tenant, membership)
    return None


def _oauth_gap(membership, connection, tokens, bindings):
    account = connection.social_account
    if account is None:
        return _gap(CredentialGapCode.OAUTH_ACCOUNT_MISSING, membership.tenant, membership)
    if account.user_id != membership.user_id:
        return _gap(CredentialGapCode.OAUTH_ACCOUNT_USER_MISMATCH, membership.tenant, membership)
    if not same_provider(account.provider, membership.tenant.provider):
        return _gap(
            CredentialGapCode.OAUTH_ACCOUNT_PROVIDER_MISMATCH,
            membership.tenant,
            membership,
        )

    if canonical_provider(membership.tenant.provider) == "ocs":
        if not connection.scope_key:
            return _gap(
                CredentialGapCode.OCS_CONNECTION_SCOPE_MISSING, membership.tenant, membership
            )
        if connection.scope_key != membership.team_slug:
            return _gap(
                CredentialGapCode.OCS_CONNECTION_SCOPE_MISMATCH,
                membership.tenant,
                membership,
            )
        identity_scope = account_scope(account)
        if not identity_scope:
            return _gap(CredentialGapCode.OCS_ACCOUNT_SCOPE_MISSING, membership.tenant, membership)
        if identity_scope != membership.team_slug:
            return _gap(
                CredentialGapCode.OCS_ACCOUNT_SCOPE_MISMATCH,
                membership.tenant,
                membership,
            )

    if not is_active_identity(account, bindings, provider=connection.provider):
        return _gap(CredentialGapCode.OAUTH_CONNECTION_INACTIVE, membership.tenant, membership)

    token = next(iter(tokens.get(account.pk, ())), None)
    if token is None:
        return _gap(CredentialGapCode.OAUTH_CREDENTIAL_MISSING, membership.tenant, membership)
    if oauth_membership_scope_mismatch(membership, connection, token.account):
        return _gap(CredentialGapCode.OAUTH_SCOPE_MISMATCH, membership.tenant, membership)
    if not token.token:
        return _gap(CredentialGapCode.OAUTH_CREDENTIAL_EMPTY, membership.tenant, membership)
    refresh_failed = bool(
        connection.oauth_refresh_failure_fingerprint
        and connection.oauth_refresh_failure_fingerprint == credential_fingerprint(token)
    )
    if refresh_failed:
        return _gap(CredentialGapCode.OAUTH_REFRESH_FAILED, membership.tenant, membership)
    health = token_health(token, connection.provider)
    can_refresh = bool(get_token_url(connection.provider) and token.token_secret and token.app)
    if health != "connected" or (
        not can_refresh and token_needs_refresh(token.expires_at, can_refresh=False)
    ):
        return _gap(CredentialGapCode.OAUTH_CREDENTIAL_EXPIRED, membership.tenant, membership)
    return None


def _membership_gap(membership, tokens, bindings, connection_teams):
    tenant = membership.tenant
    connection = membership.connection
    if connection is None:
        return _gap(CredentialGapCode.MISSING_CONNECTION, tenant, membership)
    if connection.user_id != membership.user_id:
        return _gap(CredentialGapCode.CONNECTION_USER_MISMATCH, tenant, membership)
    if not same_provider(connection.provider, tenant.provider):
        return _gap(CredentialGapCode.CONNECTION_PROVIDER_MISMATCH, tenant, membership)

    if connection.credential_type == TenantConnection.API_KEY:
        # OCS can discover and persist a key's experiments even when its
        # sessions endpoint has no team slug. The membership-to-connection link
        # is the local proof in that supported case; do not invent a team.
        return _api_key_gap(membership, connection, connection_teams)
    if connection.credential_type == TenantConnection.OAUTH:
        if canonical_provider(tenant.provider) == "ocs" and not membership.team_slug:
            return _gap(CredentialGapCode.OCS_TEAM_MISSING, tenant, membership)
        return _oauth_gap(
            membership,
            connection,
            tokens,
            bindings.get(membership.user_id, {}),
        )
    return _gap(CredentialGapCode.CREDENTIAL_TYPE_UNSUPPORTED, tenant, membership)


def _normalize_pairs(
    user_tenant_pairs: Iterable[tuple[int, Tenant]],
) -> tuple[tuple[int, Tenant], ...]:
    pairs = []
    seen = set()
    for user_id, tenant in user_tenant_pairs:
        key = (user_id, tenant.pk)
        if key in seen:
            continue
        seen.add(key)
        pairs.append((user_id, tenant))
    return tuple(pairs)


def _evaluate_tenant_readiness(
    pairs,
    tenant_memberships,
    connection_memberships,
    oauth_connections,
    tokens,
) -> tuple[TenantCredentialReadiness, ...]:
    memberships_by_user_tenant = {
        (membership.user_id, membership.tenant_id): membership for membership in tenant_memberships
    }
    tokens_by_account = defaultdict(list)
    for token in tokens:
        tokens_by_account[token.account_id].append(token)
    bindings = _bindings_by_user(oauth_connections)
    connection_teams = defaultdict(set)
    for membership in connection_memberships:
        connection_teams[membership.connection_id].add(_normalized_team_slug(membership))

    readiness = []
    for user_id, tenant in pairs:
        membership = memberships_by_user_tenant.get((user_id, tenant.pk))
        gap = (
            _gap(CredentialGapCode.MISSING_LIVE_MEMBERSHIP, tenant)
            if membership is None
            else _membership_gap(membership, tokens_by_account, bindings, connection_teams)
        )
        readiness.append(
            TenantCredentialReadiness(
                user_id=user_id,
                tenant_id=str(tenant.pk),
                usable=gap is None,
                gap=gap,
            )
        )
    return tuple(readiness)


def _workspace_reports(
    workspace_memberships,
    workspace_tenants,
    tenant_readiness,
) -> tuple[WorkspaceCredentialCoverage, ...]:
    tenants_by_workspace = defaultdict(list)
    for row in workspace_tenants:
        tenants_by_workspace[row.workspace_id].append(row.tenant)
    readiness_by_pair = {(item.user_id, item.tenant_id): item for item in tenant_readiness}
    reports = []
    for workspace_membership in workspace_memberships:
        gaps = tuple(
            item.gap
            for tenant in tenants_by_workspace[workspace_membership.workspace_id]
            if (item := readiness_by_pair[(workspace_membership.user_id, str(tenant.pk))]).gap
            is not None
        )
        reports.append(
            WorkspaceCredentialCoverage(
                workspace_id=str(workspace_membership.workspace_id),
                workspace_name=workspace_membership.workspace.name,
                user_id=workspace_membership.user_id,
                covered=not gaps,
                gaps=gaps,
            )
        )
    return tuple(reports)


def _get_tenant_credential_readiness(pairs) -> tuple[TenantCredentialReadiness, ...]:
    user_ids = {user_id for user_id, _tenant in pairs}
    tenant_ids = {tenant.pk for _user_id, tenant in pairs}
    tenant_memberships = list(_tenant_membership_queryset(user_ids, tenant_ids))
    connection_ids = {
        membership.connection_id
        for membership in tenant_memberships
        if membership.connection_id is not None
    }
    connection_memberships = list(_connection_membership_queryset(connection_ids))
    oauth_connections = list(_oauth_connection_queryset(user_ids))
    account_ids = {
        connection.social_account_id
        for connection in oauth_connections
        if connection.social_account_id is not None
    }
    tokens = list(_token_queryset(account_ids))
    return _evaluate_tenant_readiness(
        pairs,
        tenant_memberships,
        connection_memberships,
        oauth_connections,
        tokens,
    )


def get_tenant_credential_readiness(
    user_tenant_pairs: Iterable[tuple[int, Tenant]],
) -> tuple[TenantCredentialReadiness, ...]:
    """Evaluate explicit user/tenant pairs without requiring workspace rows.

    ``Tenant`` instances carry the affected source metadata into structured
    gaps. Callers can use this for proposed membership changes before writing
    either a ``WorkspaceMembership`` or ``WorkspaceTenant``. Duplicate pairs
    are evaluated once in first-occurrence order.
    """
    return _get_tenant_credential_readiness(_normalize_pairs(user_tenant_pairs))


def get_workspace_credential_coverage(
    *,
    workspace_ids: Iterable | None = None,
    user_ids: Iterable[int] | None = None,
) -> tuple[WorkspaceCredentialCoverage, ...]:
    """Return local credential readiness for selected workspace members.

    The query count is bounded independently of the number of workspaces,
    members, tenants, or credentials in the result. Memory use grows with the
    selected inventory, so large audits should pass workspace or user filters.
    """
    workspace_memberships = list(
        _workspace_membership_queryset(workspace_ids=workspace_ids, user_ids=user_ids)
    )
    workspace_ids = {membership.workspace_id for membership in workspace_memberships}
    user_ids = {membership.user_id for membership in workspace_memberships}
    workspace_tenants = list(_workspace_tenant_queryset(workspace_ids))
    tenants_by_workspace = defaultdict(list)
    for row in workspace_tenants:
        tenants_by_workspace[row.workspace_id].append(row.tenant)
    pairs = _normalize_pairs(
        (membership.user_id, tenant)
        for membership in workspace_memberships
        for tenant in tenants_by_workspace[membership.workspace_id]
    )
    readiness = _get_tenant_credential_readiness(pairs)
    return _workspace_reports(
        workspace_memberships,
        workspace_tenants,
        readiness,
    )


async def _alist(queryset):
    return [value async for value in queryset]


async def _aget_tenant_credential_readiness(pairs) -> tuple[TenantCredentialReadiness, ...]:
    user_ids = {user_id for user_id, _tenant in pairs}
    tenant_ids = {tenant.pk for _user_id, tenant in pairs}
    tenant_memberships = await _alist(_tenant_membership_queryset(user_ids, tenant_ids))
    connection_ids = {
        membership.connection_id
        for membership in tenant_memberships
        if membership.connection_id is not None
    }
    connection_memberships = await _alist(_connection_membership_queryset(connection_ids))
    oauth_connections = await _alist(_oauth_connection_queryset(user_ids))
    account_ids = {
        connection.social_account_id
        for connection in oauth_connections
        if connection.social_account_id is not None
    }
    tokens = await _alist(_token_queryset(account_ids))
    return _evaluate_tenant_readiness(
        pairs,
        tenant_memberships,
        connection_memberships,
        oauth_connections,
        tokens,
    )


async def aget_tenant_credential_readiness(
    user_tenant_pairs: Iterable[tuple[int, Tenant]],
) -> tuple[TenantCredentialReadiness, ...]:
    """Async twin of :func:`get_tenant_credential_readiness`."""
    return await _aget_tenant_credential_readiness(_normalize_pairs(user_tenant_pairs))


async def aget_workspace_credential_coverage(
    *,
    workspace_ids: Iterable | None = None,
    user_ids: Iterable[int] | None = None,
) -> tuple[WorkspaceCredentialCoverage, ...]:
    """Async twin of :func:`get_workspace_credential_coverage`."""
    workspace_memberships = await _alist(
        _workspace_membership_queryset(workspace_ids=workspace_ids, user_ids=user_ids)
    )
    workspace_ids = {membership.workspace_id for membership in workspace_memberships}
    user_ids = {membership.user_id for membership in workspace_memberships}
    workspace_tenants = await _alist(_workspace_tenant_queryset(workspace_ids))
    tenants_by_workspace = defaultdict(list)
    for row in workspace_tenants:
        tenants_by_workspace[row.workspace_id].append(row.tenant)
    pairs = _normalize_pairs(
        (membership.user_id, tenant)
        for membership in workspace_memberships
        for tenant in tenants_by_workspace[membership.workspace_id]
    )
    readiness = await _aget_tenant_credential_readiness(pairs)
    return _workspace_reports(
        workspace_memberships,
        workspace_tenants,
        readiness,
    )


class CoverageRecovery(StrEnum):
    """What a member has to do to become usable for one tenant.

    The contract distinguishes these because the fixes differ: a member who
    never had the source must connect it, a member whose access was removed
    must have it restored upstream, an expired sign-in only needs a reconnect,
    and an OCS credential for the wrong or an unknown team needs that team
    connected specifically.
    """

    CONNECT_SOURCE = "connect_source"
    ACCESS_REMOVED = "access_removed"
    RECONNECT = "reconnect"
    CONNECT_TEAM = "connect_team"
    LEGACY_TEAM_UNKNOWN = "legacy_team_unknown"


_TEAM_GAPS = frozenset(
    {
        CredentialGapCode.OCS_API_KEY_TEAM_AMBIGUOUS,
        CredentialGapCode.OAUTH_SCOPE_MISMATCH,
        CredentialGapCode.OCS_CONNECTION_SCOPE_MISSING,
        CredentialGapCode.OCS_CONNECTION_SCOPE_MISMATCH,
        CredentialGapCode.OCS_ACCOUNT_SCOPE_MISSING,
        CredentialGapCode.OCS_ACCOUNT_SCOPE_MISMATCH,
    }
)


@dataclass(frozen=True)
class MissingTenant:
    """One workspace tenant a member cannot currently use, with its remedy.

    Carries only source identity and team labels — never credential material —
    because it is returned to the member being denied, and to a manager told which
    members cannot use a source they are adding.
    """

    tenant_id: str
    tenant_name: str
    provider: str
    recovery: CoverageRecovery
    team_slug: str = ""
    team_name: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _recovery(item: TenantCredentialReadiness, removed_pairs) -> CoverageRecovery:
    gap = item.gap
    if gap.code == CredentialGapCode.MISSING_LIVE_MEMBERSHIP:
        if (item.user_id, item.tenant_id) in removed_pairs:
            return CoverageRecovery.ACCESS_REMOVED
        return CoverageRecovery.CONNECT_SOURCE
    if gap.code == CredentialGapCode.OCS_TEAM_MISSING:
        return CoverageRecovery.LEGACY_TEAM_UNKNOWN
    if gap.code in _TEAM_GAPS:
        return CoverageRecovery.CONNECT_TEAM
    return CoverageRecovery.RECONNECT


def _missing_tenant(item: TenantCredentialReadiness, removed_pairs) -> MissingTenant:
    return MissingTenant(
        tenant_id=item.gap.tenant_id,
        tenant_name=item.gap.tenant_name,
        provider=item.gap.provider,
        recovery=_recovery(item, removed_pairs),
        team_slug=item.gap.team_slug,
        team_name=item.gap.team_name,
    )


def _unmembered(readiness) -> list[TenantCredentialReadiness]:
    return [
        item
        for item in readiness
        if item.gap is not None and item.gap.code == CredentialGapCode.MISSING_LIVE_MEMBERSHIP
    ]


def _removed_pairs_queryset(unmembered):
    # An archived row is the tombstone an authoritative upstream denial (or the
    # member disconnecting that account) leaves behind; no row means never had.
    return TenantMembership.all_objects.filter(
        user_id__in={item.user_id for item in unmembered},
        tenant_id__in={item.tenant_id for item in unmembered},
        archived_at__isnull=False,
    ).values_list("user_id", "tenant_id")


def _gaps_by_pair(readiness, removed_pairs) -> dict[tuple[int, str], MissingTenant]:
    removed = {(user_id, str(tenant_id)) for user_id, tenant_id in removed_pairs}
    return {
        (item.user_id, item.tenant_id): _missing_tenant(item, removed)
        for item in readiness
        if item.gap is not None
    }


def coverage_gaps(
    user_tenant_pairs: Iterable[tuple[int, Tenant]],
) -> dict[tuple[int, str], MissingTenant]:
    """Pairs whose user cannot use the tenant with their own credential.

    Keyed by ``(user_id, tenant id string)``; absent pairs are covered. This is
    the all-of predicate behind the workspace authorizer and admission checks.
    It evaluates local readiness only and never borrows another user's
    credential: one user's coverage says nothing about another's.
    """
    readiness = get_tenant_credential_readiness(user_tenant_pairs)
    unmembered = _unmembered(readiness)
    removed = list(_removed_pairs_queryset(unmembered)) if unmembered else []
    return _gaps_by_pair(readiness, removed)


async def acoverage_gaps(
    user_tenant_pairs: Iterable[tuple[int, Tenant]],
) -> dict[tuple[int, str], MissingTenant]:
    """Async twin of :func:`coverage_gaps`."""
    readiness = await aget_tenant_credential_readiness(user_tenant_pairs)
    unmembered = _unmembered(readiness)
    removed = await _alist(_removed_pairs_queryset(unmembered)) if unmembered else []
    return _gaps_by_pair(readiness, removed)


def member_coverage_gaps(user_id: int, tenants: Iterable[Tenant]) -> dict[str, MissingTenant]:
    """:func:`coverage_gaps` for one user, keyed by tenant id string."""
    gaps = coverage_gaps((user_id, tenant) for tenant in tenants)
    return {tenant_id: missing for (_user_id, tenant_id), missing in gaps.items()}


async def amember_coverage_gaps(
    user_id: int, tenants: Iterable[Tenant]
) -> dict[str, MissingTenant]:
    """Async twin of :func:`member_coverage_gaps`."""
    gaps = await acoverage_gaps((user_id, tenant) for tenant in tenants)
    return {tenant_id: missing for (_user_id, tenant_id), missing in gaps.items()}
