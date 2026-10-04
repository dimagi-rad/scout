"""Workspace management API views."""

import asyncio
import logging
from dataclasses import dataclass
from typing import NamedTuple

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, OuterRef, Subquery
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.chat.models import Thread
from apps.common.errors import (
    CommCareAuthError,
    ConnectAuthError,
    OCSAuthError,
    TokenRefreshError,
)
from apps.common.http import string_field
from apps.users.models import Tenant, TenantMembership
from apps.users.services.credential_resolver import aiter_social_tokens
from apps.users.services.email_proof import proven_emails
from apps.users.services.oauth_scope import account_scope
from apps.users.services.tenant_resolution import (
    resolve_commcare_domains,
    resolve_connect_opportunities,
    resolve_ocs_chatbots,
)
from apps.users.services.token_refresh import (
    WORKER_DB_DEADLINE,
    TokenRefreshUnavailable,
    get_token_url,
    refresh_oauth_token,
    token_needs_refresh,
)
from apps.workspaces.access import (
    missing_tenants_by_workspace,
    missing_tenants_for_member,
    missing_tenants_payload,
    needed_text,
    remedy_text,
)
from apps.workspaces.models import (
    LIVE_INVITE_STATUSES,
    Workspace,
    WorkspaceInvite,
    WorkspaceInviteStatus,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    default_invite_expiry,
)
from apps.workspaces.services.credential_coverage import (
    CoverageRecovery,
    oauth_token_counts_as_expired,
)
from apps.workspaces.services.invite_notifications import (
    notify_awaiting_access,
    notify_invite_revoked,
    notify_member_added,
    notify_member_removed,
    notify_role_changed,
    send_pending_invite_email,
)
from apps.workspaces.services.load_activity import workspace_schema_statuses
from apps.workspaces.services.load_progress import workspace_ids_in_progress
from apps.workspaces.services.member_coverage import (
    MembersLackTenant,
    add_tenant_covered_by_members,
    admit_covered_member,
    members_lacking_tenant,
    missing_for_user,
    requester_gaps,
)
from apps.workspaces.services.query_state import synced_runs
from apps.workspaces.services.workspace_service import (
    LastWorkspaceTenant,
    load_new_workspace,
    remove_workspace_tenant,
)
from apps.workspaces.workspace_resolver import resolve_workspace_drf as resolve_workspace

logger = logging.getLogger(__name__)

# Bounded so a slow upstream export can't tie up the sync DRF worker thread.
SHARE_REFRESH_TIMEOUT = 8  # seconds
# The whole renew pass for a directly added user, across providers and identities.
# A renewal already under way still runs to completion (it must never be cancelled
# mid-rotation), so this bounds what starts, not a hard wall-clock ceiling.
TARGET_REFRESH_BUDGET = 2 * SHARE_REFRESH_TIMEOUT

_PROVIDER_RESOLVERS = {
    "commcare": resolve_commcare_domains,
    "commcare_connect": resolve_connect_opportunities,
    "ocs": resolve_ocs_chatbots,
}

_UPSTREAM_AUTH_ERRORS = (CommCareAuthError, ConnectAuthError, OCSAuthError)


@dataclass(frozen=True)
class Rediscovery:
    """What rediscovering one user's access with their stored sign-ins found."""

    # The check couldn't settle coverage: a provider error, a timeout, or a refusal
    # that proves nothing (Connect's export-list 403). Retrying may help.
    failed: bool = False
    # A stored sign-in upstream refused (401), or one that can't be renewed and that
    # admission counts as expired: only the user signing in again settles it.
    needs_sign_in: bool = False


class MemberRecheck(NamedTuple):
    complete: bool = True
    needs_sign_in: frozenset = frozenset()


async def _aunexpired_access_tokens(user, provider) -> list[tuple]:
    """``(identity, access token)`` pairs usable as-is, without renewing any.

    For refreshes a *different* user triggers: renewing someone else's token can
    record a refresh failure on a transient provider error, which would take away
    access they currently have. An unrecorded expiry counts as usable, as it does
    in the coverage check this rediscovery feeds.
    """
    now = timezone.now()
    return [
        (token.account, token.token)
        for token in await aiter_social_tokens(user, provider)
        if _usable_as_is(token, now)
    ]


def _usable_as_is(token, now) -> bool:
    return bool(token.token) and not (token.expires_at and token.expires_at <= now)


async def _arenewed_access_tokens(user, provider, deadline) -> tuple[list[tuple], Rediscovery]:
    """``(pairs, outcome)``: *user*'s tokens, renewing any that need it.

    Only for the user a request names (G2). They did not start the request, so a
    failed renewal records nothing on their credential (G1). A token that isn't
    renewed is used as it stands while unexpired. When one leaves nothing to use,
    ``outcome`` says why: ``failed`` for a transient failure or the ``deadline``
    (event-loop time) passing before a renewal starts, ``needs_sign_in`` when the
    provider refused to renew it. A token that cannot be renewed and that admission
    counts as expired sets ``needs_sign_in`` even while it is still used.
    """
    now = timezone.now()
    pairs, failed, needs_sign_in = [], False, False
    for token in await aiter_social_tokens(user, provider):
        # Per identity: a user's www and EU CommCare grants refresh at different servers.
        scope_key = account_scope(token.account)
        token_url = get_token_url(provider, scope_key)
        stored = (token.account, token.token) if _usable_as_is(token, now) else None
        can_refresh = bool(token_url and token.token_secret and token.app)
        if not can_refresh or not token_needs_refresh(token.expires_at):
            if stored:
                pairs.append(stored)
            # Still used while it lists anything, but admission will refuse it anyway.
            if not stored or oauth_token_counts_as_expired(token, provider, scope_key):
                needs_sign_in = True
            continue
        if asyncio.get_running_loop().time() >= deadline:
            if stored:
                pairs.append(stored)
            else:
                failed = True
            continue
        try:
            # Bounded by its own timeouts, never cancelled: cancelling between the
            # provider rotating the grant and Scout storing it would lose the grant.
            # The worker deadline for the same reason: the grant is the target's, so
            # the manager waits out a busy row rather than the target reconnecting.
            access_token = await refresh_oauth_token(
                token,
                token_url,
                request_timeout=SHARE_REFRESH_TIMEOUT,
                db_timeout=WORKER_DB_DEADLINE,
                record_failure=False,
            )
        except TokenRefreshError as error:
            if stored:
                pairs.append(stored)
            elif isinstance(error, TokenRefreshUnavailable):
                failed = True
            else:
                # Any non-transient refusal is what a user's own refresh records as
                # "reconnect required"; here it is only reported, never recorded (G1).
                needs_sign_in = True
            continue
        pairs.append((token.account, access_token))
    return pairs, Rediscovery(failed=failed, needs_sign_in=needs_sign_in)


async def _arefresh_target_for_workspace(target, providers, *, renew=False) -> Rediscovery:
    """Best-effort, bounded server-side refresh of *target*'s memberships for the
    workspace's tenant providers, using the target's OWN tokens.

    This is what lets a manager add someone who was granted access upstream after
    the target's last Scout login — without the target manually reconnecting.
    ``renew`` renews expired tokens first, for the named target of an add only;
    anyone else's tokens are used as they stand (see ``_aunexpired_access_tokens``).

    Additive only (``may_revoke=False``): the target did not start this request, so
    nothing it observes may archive their access or record a denial (#561 G1). A
    refused sign-in is reported as ``needs_sign_in`` instead.

    Every identity per provider is refreshed. A target holding two OCS teams has
    a token per team, and refreshing only one would report them as not covering a
    tenant they can in fact reach — the false negative multi-token OAuth exists
    to remove (#156).
    """
    loop = asyncio.get_running_loop()
    # The member fan-out bounds the non-renew pass as a whole; renew runs inline.
    deadline = loop.time() + TARGET_REFRESH_BUDGET if renew else None
    failed = needs_sign_in = False
    for provider in providers:
        resolve = _PROVIDER_RESOLVERS.get(provider)
        if resolve is None:
            continue
        if renew:
            pairs, renewal = await _arenewed_access_tokens(target, provider, deadline)
            failed = failed or renewal.failed
            needs_sign_in = needs_sign_in or renewal.needs_sign_in
        else:
            pairs = await _aunexpired_access_tokens(target, provider)
        for account, token in pairs:
            timeout = SHARE_REFRESH_TIMEOUT
            if deadline is not None:
                timeout = min(timeout, deadline - loop.time())
                if timeout <= 0:
                    failed = True
                    break
            try:
                await asyncio.wait_for(
                    resolve(
                        target,
                        token,
                        social_account=account,
                        allow_replace=False,
                        may_revoke=False,
                    ),
                    timeout=timeout,
                )
            except _UPSTREAM_AUTH_ERRORS as refused:
                # A 403 is upstream withholding access, which signing in again cannot
                # change (#372): it stays a plain "not covered", neither flag set.
                # Connect's export-list 403 is the exception: it can refuse a user who
                # still holds the opportunity (see tenant_resolution), so it proves
                # nothing and reads as a check that couldn't finish.
                needs_sign_in = needs_sign_in or refused.status_code == 401
                if isinstance(refused, ConnectAuthError) and refused.status_code == 403:
                    failed = True
                logger.info(
                    "Share-time refresh refused upstream (HTTP %s) for target=%s "
                    "provider=%s account=%s",
                    refused.status_code,
                    target.id,
                    provider,
                    account.pk,
                )
            except Exception:
                failed = True
                logger.warning(
                    "Share-time refresh failed for target=%s provider=%s account=%s",
                    target.id,
                    provider,
                    account.pk,
                    exc_info=True,
                )
    return Rediscovery(failed=failed, needs_sign_in=needs_sign_in)


# Bounds the provider fan-out and keeps queued refreshes from spending their
# timeout waiting on the (serialized) persistence legs of the ones ahead.
MEMBER_REFRESH_CONCURRENCY = 4
# Whole fan-out, so a large workspace can't hold a sync worker indefinitely.
MEMBER_REFRESH_BUDGET = 2 * SHARE_REFRESH_TIMEOUT


async def _arefresh_members_for_provider(users, provider) -> MemberRecheck:
    """Best-effort rediscovery with each user's own, still-valid identities.

    Advisory only: the locked coverage check decides, so one member's failure
    must neither fail the request nor stop the others. Tokens are not renewed and
    nothing is revoked: a manager's click must never mark another member's
    credential as failed or archive their access.

    ``complete`` is False if some member's rediscovery failed or ran out of time,
    since then retrying may give a different answer. ``needs_sign_in`` holds the
    ids of members whose stored sign-in upstream refused.
    """
    gate = asyncio.Semaphore(MEMBER_REFRESH_CONCURRENCY)

    async def refresh(user):
        async with gate:
            return await _arefresh_target_for_workspace(user, [provider])

    tasks = [asyncio.ensure_future(refresh(user)) for user in users]
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=MEMBER_REFRESH_BUDGET
        )
    except TimeoutError:
        logger.warning(
            "Source-add refresh ran out of time for provider=%s (%d members)",
            provider,
            len(users),
        )
    complete = True
    needs_sign_in = set()
    for user, task in zip(users, tasks, strict=True):
        if not task.done() or task.cancelled():
            complete = False
            continue
        error = task.exception()
        if error is not None:
            complete = False
            logger.warning(
                "Source-add refresh failed for member=%s provider=%s",
                user.id,
                provider,
                exc_info=error,
            )
            continue
        outcome = task.result()
        complete = complete and not outcome.failed
        if outcome.needs_sign_in:
            needs_sign_in.add(user.pk)
    return MemberRecheck(complete=complete, needs_sign_in=frozenset(needs_sign_in))


def _member_label(user) -> str:
    name = user.get_full_name()
    return f"{name} <{user.email}>" if name else user.email


def _members_lack_source_body(tenant, gaps, recheck: MemberRecheck) -> dict:
    names = ", ".join(_member_label(user) for user, _missing in gaps)
    unchecked = (
        ""
        if recheck.complete
        else " Scout couldn't finish rechecking some members upstream, so retrying may help."
    )
    refused = [user for user, _missing in gaps if user.pk in recheck.needs_sign_in]
    if refused:
        unchecked += (
            f" Scout couldn't recheck {', '.join(_member_label(user) for user in refused)} "
            "with their saved sign-in; they need to sign in to Scout again."
        )
    return {
        "error": (
            f"Can't add '{tenant.canonical_name}': {names} can't use it with their own "
            "account yet, and every member must be able to use every source. They can "
            "connect it in Connected Accounts once they have access to it; otherwise "
            "remove them from this workspace or create a separate workspace for this source."
            + unchecked
        ),
        "reason": "members_lack_source",
        "recheck_complete": recheck.complete,
        "members": [
            {
                "user_id": str(user.id),
                "email": user.email,
                "name": user.get_full_name(),
                "needs_sign_in": user.pk in recheck.needs_sign_in,
                **missing_tenants_payload([missing])[0],
            }
            for user, missing in gaps
        ],
    }


def _name_error(name) -> str | None:
    """Why ``name`` can't be a workspace name, or None; shared by create and rename."""
    if not isinstance(name, str):
        return "name must be a string."
    name_limit = Workspace._meta.get_field("name").max_length
    if len(name.strip()) > name_limit:
        return f"name must be {name_limit} characters or fewer."
    return None


def _is_last_manager(workspace, membership):
    """Return True if membership is the sole manager of workspace."""
    if membership.role != WorkspaceRole.MANAGE:
        return False
    return workspace.memberships.filter(role=WorkspaceRole.MANAGE).count() <= 1


def _serialize_invite(invite, result=None):
    payload = {
        "id": str(invite.id),
        "email": invite.email,
        "role": invite.role,
        "status": invite.status,
        "created_at": invite.created_at.isoformat(),
    }
    if result is not None:
        payload["result"] = result
    return payload


LIVE_INVITE_CONSTRAINT = "one_live_invite_per_workspace_email"


def _update_if_live(invite, **fields) -> bool:
    return bool(
        WorkspaceInvite.objects.filter(pk=invite.pk, status__in=LIVE_INVITE_STATUSES).update(
            **fields
        )
    )


def _invite_no_longer_live():
    return Response({"error": "Invite is no longer live."}, status=status.HTTP_409_CONFLICT)


def _upsert_invite(workspace, email, role, invited_by, new_status):
    """Create or refresh the single live invite for (workspace, email).

    Re-inviting an outstanding invite is idempotent — it updates role/expiry and
    the pending↔awaiting_access status rather than violating the
    one-live-invite-per-(workspace,email) constraint. A stale (expired) live
    invite is retired to EXPIRED first so a fresh one can take its place.

    Returns None when a concurrent request resolved or replaced the live invite.
    """
    live = WorkspaceInvite.objects.filter(
        workspace=workspace, email=email, status__in=LIVE_INVITE_STATUSES
    ).first()
    # Conditional writes: a login may accept (or a manager revoke) the invite
    # after the read above, and a live status must not be written back (#561 G4).
    if live and not live.is_expired:
        fields = {
            "role": role,
            "invited_by": invited_by,
            "status": new_status,
            "expires_at": default_invite_expiry(),
            "updated_at": timezone.now(),
        }
        if not _update_if_live(live, **fields):
            return None
        for name, value in fields.items():
            setattr(live, name, value)
        return live
    if live and live.is_expired:
        # Best-effort: a login may have retired this row already.
        _update_if_live(live, status=WorkspaceInviteStatus.EXPIRED, updated_at=timezone.now())
    try:
        # Savepoint, so a lost race doesn't poison an enclosing transaction.
        with transaction.atomic():
            return WorkspaceInvite.objects.create(
                workspace=workspace,
                email=email,
                role=role,
                invited_by=invited_by,
                status=new_status,
            )
    except IntegrityError as exc:
        diag = getattr(exc.__cause__, "diag", None)
        if getattr(diag, "constraint_name", None) != LIVE_INVITE_CONSTRAINT:
            raise
        # A concurrent re-invite created the live invite first.
        return None


def _workspace_delete_refusal(user, workspace, *, last_source=False) -> Response | None:
    """Why the manager ``user`` may not delete ``workspace``, or None if they may.

    Shared by workspace delete and removing its last source (#381), which is the
    same destruction and must be refused in the same cases.
    """
    # Deleting destroys every member's content, so without coverage it is a
    # remediation only for a workspace nobody else is in.
    missing = {t.tenant_id for t in missing_tenants_for_member(user, workspace)}
    # authz-exempt: counts the OTHER members; the requester was already admitted.
    if missing and workspace.memberships.exclude(user=user).exists():
        return Response(
            {
                "error": "You can't delete a shared workspace while you're missing one "
                "of its sources. "
                + (
                    "Make another member a manager and leave instead."
                    if last_source
                    else "Remove that source, or make another member a manager and leave."
                )
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    # Check this is not the user's last workspace covering any tenant. A source
    # they can no longer use isn't "covered" by keeping this workspace, and
    # counting it would trap them in a workspace they can't open.
    tenant_ids = [
        tid
        for tid in workspace.workspace_tenants.values_list("tenant_id", flat=True)
        if str(tid) not in missing
    ]
    for tid in tenant_ids:
        # authz-exempt: lists the user's OTHER workspaces; this one was already admitted.
        other_workspaces = Workspace.objects.filter(
            workspace_tenants__tenant_id=tid,
            memberships__user=user,
        ).exclude(id=workspace.id)
        if not other_workspaces.exists():
            return Response(
                {
                    "error": "Cannot delete your last workspace covering a tenant. "
                    "Create another workspace for that tenant first."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
    return None


class WorkspaceListView(APIView):
    """
    GET  /api/workspaces/  — list workspaces the authenticated user is a member of.
    POST /api/workspaces/  — create a new workspace.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        latest_run = (
            synced_runs()
            .filter(tenant_schema__tenant__workspace_tenants__workspace=OuterRef("workspace"))
            .values("completed_at")[:1]
        )

        memberships = (
            WorkspaceMembership.objects.filter(user=request.user)
            .select_related("workspace")
            .prefetch_related("workspace__workspace_tenants__tenant")
            .annotate(
                member_count=Count("workspace__memberships", distinct=True),
                last_synced_at=Subquery(latest_run),
            )
            .order_by("-workspace__created_at", "-workspace_id")
        )
        memberships = list(memberships)
        schema_statuses = workspace_schema_statuses(m.workspace_id for m in memberships)
        loading_ids = workspace_ids_in_progress(m.workspace_id for m in memberships)

        # Surfaced per row (rather than filtering rows out) so the client can keep
        # denied workspaces addressable by URL while gating them in the UI and
        # telling the member which sources to connect.
        missing_by_ws = missing_tenants_by_workspace(
            request.user, [m.workspace for m in memberships]
        )

        results = []
        for m in memberships:
            workspace_tenants = [wt.tenant for wt in m.workspace.workspace_tenants.all()]
            tenants = [
                {
                    "id": str(tenant.id),
                    "tenant_name": tenant.canonical_name,
                    "provider": tenant.provider,
                }
                for tenant in workspace_tenants
            ]
            missing = missing_by_ws[m.workspace.id]
            results.append(
                {
                    "id": str(m.workspace.id),
                    "name": m.workspace.name,
                    "display_name": m.workspace.display_name_for(workspace_tenants),
                    "is_auto_created": m.workspace.is_auto_created,
                    "role": m.role,
                    "tenants": tenants,
                    # No sources is denied by the gate (#381) without naming any missing.
                    "has_access": bool(workspace_tenants) and not missing,
                    "missing_tenants": missing_tenants_payload(missing),
                    "member_count": m.member_count,
                    "schema_status": schema_statuses[m.workspace_id],
                    "in_progress": m.workspace_id in loading_ids,
                    "last_synced_at": (m.last_synced_at.isoformat() if m.last_synced_at else None),
                    "created_at": m.workspace.created_at.isoformat(),
                }
            )
        return Response(results)

    def post(self, request):
        name = request.data.get("name", "")
        if error := _name_error(name):
            return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
        name = name.strip()
        if not name:
            return Response({"error": "name is required."}, status=status.HTTP_400_BAD_REQUEST)

        tenant_ids = request.data.get("tenant_ids", [])
        if not isinstance(tenant_ids, list):
            return Response(
                {"error": "tenant_ids must be a list."}, status=status.HTTP_400_BAD_REQUEST
            )
        if not tenant_ids:
            return Response(
                {"error": "Choose at least one data source for the workspace."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        accessible_tenant_ids = set(
            str(tid)
            for tid in TenantMembership.objects.filter(user=request.user).values_list(
                "tenant_id", flat=True
            )
        )
        for tid in tenant_ids:
            if str(tid) not in accessible_tenant_ids:
                return Response(
                    {"error": "One or more tenants are not accessible."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        selected = list(Tenant.objects.filter(id__in=tenant_ids))
        missing = requester_gaps(request.user, selected)
        if missing:
            return Response(
                {
                    "error": (
                        "You can't use every selected source with your own account yet. "
                        f"Still needed — {needed_text(missing_tenants_payload(missing))}."
                    ),
                    "missing_tenants": missing_tenants_payload(missing),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            workspace = Workspace.objects.create(
                name=name,
                is_auto_created=False,
                created_by=request.user,
            )
            for tenant in selected:
                WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)
            WorkspaceMembership.objects.create(
                workspace=workspace,
                user=request.user,
                role=WorkspaceRole.MANAGE,
            )
            load_new_workspace(workspace, actor_id=request.user.id)
        tenants = [
            {
                "id": str(tenant.id),
                "tenant_name": tenant.canonical_name,
                "provider": tenant.provider,
            }
            for tenant in selected
        ]
        return Response(
            {
                "id": str(workspace.id),
                "name": workspace.name,
                "display_name": workspace.display_name_for(selected),
                "is_auto_created": workspace.is_auto_created,
                "role": WorkspaceRole.MANAGE,
                "tenants": tenants,
                "member_count": 1,
                "created_at": workspace.created_at.isoformat(),
            },
            status=status.HTTP_201_CREATED,
        )


class WorkspaceDetailView(APIView):
    """
    GET    /api/workspaces/<workspace_id>/  — workspace detail.
    PATCH  /api/workspaces/<workspace_id>/  — rename (manage only).
    DELETE /api/workspaces/<workspace_id>/  — delete (manage only).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        # Metadata only, and the page that offers remove-source/leave/delete loads
        # it first, so it must stay reachable without coverage.
        workspace, membership, err = resolve_workspace(
            request, workspace_id, require_coverage=False
        )
        if err:
            return err
        missing = missing_tenants_for_member(request.user, workspace)

        tenants = list(workspace.tenants.all())
        schema_status = workspace_schema_statuses([workspace.id])[workspace.id]

        last_run_at = (
            synced_runs()
            .filter(tenant_schema__tenant__in=tenants)
            .values_list("completed_at", flat=True)
            .first()
        )
        last_synced_at = last_run_at.isoformat() if last_run_at else None

        return Response(
            {
                "id": str(workspace.id),
                "name": workspace.name,
                "display_name": workspace.display_name_for(tenants),
                "is_auto_created": workspace.is_auto_created,
                "role": membership.role,
                # Agent configuration is workspace content, not page metadata.
                "system_prompt": "" if missing else workspace.system_prompt,
                "missing_tenants": missing_tenants_payload(missing),
                "schema_status": schema_status,
                "in_progress": bool(workspace_ids_in_progress([workspace.id])),
                "tenant_count": len(tenants),
                "member_count": workspace.memberships.count(),
                "created_at": workspace.created_at.isoformat(),
                "updated_at": workspace.updated_at.isoformat(),
                "last_synced_at": last_synced_at,
            }
        )

    def patch(self, request, workspace_id):
        # Must keep requiring coverage: get() blanks system_prompt for an uncovered
        # member, so accepting their write would let them save that blank over it.
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        if membership.role != WorkspaceRole.MANAGE:
            return Response(
                {"error": "Only workspace managers can rename a workspace."},
                status=status.HTTP_403_FORBIDDEN,
            )

        name = request.data.get("name", "")
        if error := _name_error(name):
            return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
        system_prompt = request.data.get("system_prompt")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return Response(
                {"error": "system_prompt must be a string."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        name = name.strip()
        if name:
            workspace.name = name
        if system_prompt is not None:
            if len(system_prompt) > 10_000:
                return Response(
                    {"error": "system_prompt must be 10,000 characters or fewer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            workspace.system_prompt = system_prompt

        workspace.save(update_fields=["name", "system_prompt", "updated_at"])
        return Response(
            {
                "id": str(workspace.id),
                "name": workspace.name,
                "display_name": workspace.display_name,
            }
        )

    def delete(self, request, workspace_id):
        workspace, membership, err = resolve_workspace(
            request, workspace_id, require_coverage=False
        )
        if err:
            return err
        if membership.role != WorkspaceRole.MANAGE:
            return Response(
                {"error": "Only workspace managers can delete a workspace."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if refusal := _workspace_delete_refusal(request.user, workspace):
            return refusal
        workspace.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkspaceMemberListView(APIView):
    """
    GET  /api/workspaces/<workspace_id>/members/  — list members (any member).
    POST /api/workspaces/<workspace_id>/members/  — add an existing user as a member (manage only).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        # The only source of the membership ids that leaving and handing over the
        # manager role need, so reachable without coverage. Uncovered, a non-manager
        # sees only themselves; a manager also sees who the others are, since handing
        # over blind is no escape hatch (A3). The roster is Scout's own data, not
        # tenant data; user ids, join dates and invites stay covered-only.
        workspace, membership, err = resolve_workspace(
            request, workspace_id, require_coverage=False
        )
        if err:
            return err
        covered = not missing_tenants_for_member(request.user, workspace)

        memberships = WorkspaceMembership.objects.filter(workspace=workspace).select_related("user")
        if not covered and membership.role != WorkspaceRole.MANAGE:
            memberships = memberships.filter(user=request.user)
        members = [
            {
                "id": str(m.id),
                "user_id": str(m.user.id),
                "email": m.user.email,
                "name": m.user.get_full_name(),
                "role": m.role,
                "created_at": m.created_at.isoformat(),
            }
            if covered or m.user_id == request.user.id
            else {
                "id": str(m.id),
                "role": m.role,
                "email": m.user.email,
                "name": m.user.get_full_name(),
            }
            for m in memberships
        ]
        live_invites = WorkspaceInvite.objects.filter(
            workspace=workspace,
            status__in=LIVE_INVITE_STATUSES,
            expires_at__gt=timezone.now(),
        )
        invites = [_serialize_invite(i) for i in live_invites] if covered else []
        return Response({"members": members, "invites": invites})

    def post(self, request, workspace_id):
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        if membership.role != WorkspaceRole.MANAGE:
            return Response(
                {"error": "Only managers can add members."},
                status=status.HTTP_403_FORBIDDEN,
            )

        email, err = string_field(request.data, "email")
        if err:
            return err
        email = email.strip().lower()
        if not email or "@" not in email:
            return Response({"error": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

        role = request.data.get("role")
        if role not in WorkspaceRole.values:
            return Response({"error": "Invalid role."}, status=status.HTTP_400_BAD_REQUEST)

        target = get_user_model().objects.filter(email__iexact=email).first()

        # No Scout account yet → pure pre-authorization; resolves on their first login.
        if target is None:
            invite = _upsert_invite(
                workspace, email, role, request.user, WorkspaceInviteStatus.PENDING
            )
            if invite is None:
                return _invite_no_longer_live()
            send_pending_invite_email(invite)
            return Response(
                _serialize_invite(invite, result="invite_pending"),
                status=status.HTTP_201_CREATED,
            )

        # authz-exempt: skip the refresh for an existing member; admission answers 409.
        already_member = WorkspaceMembership.objects.filter(
            workspace=workspace, user=target
        ).exists()
        gaps = () if already_member else missing_for_user(target, workspace)
        rediscovery = Rediscovery()
        if gaps:
            # The target may have been granted access upstream (Connect/HQ/OCS)
            # after their last Scout login. Refresh their memberships server-side
            # using their own token, renewed if it has expired, then re-check.
            providers = sorted({t.provider for t in gaps})
            # DRF APIView is sync; the refresh is async provider I/O.
            rediscovery = async_to_sync(_arefresh_target_for_workspace)(
                target, providers, renew=True
            )

        # Every member must cover every source (#381), so a target still missing
        # one after the refresh gets an invite that awaits it rather than a hard
        # failure: it resolves once they can use every source and sign in.
        new_membership, created, missing = admit_covered_member(
            workspace, target, role=role, invited_by=request.user
        )
        if missing:
            invite = _upsert_invite(
                workspace, email, role, request.user, WorkspaceInviteStatus.AWAITING_ACCESS
            )
            if invite is None:
                return _invite_no_longer_live()
            # The invitee already has a Scout account, so they never get the
            # pending-invite email; tell them directly they need upstream access.
            # The manager just performed this action, so don't email them.
            notify_awaiting_access(invite, target, notify_manager=False)
            return Response(
                {
                    **_serialize_invite(invite, result="invite_awaiting_access"),
                    "recheck_complete": not rediscovery.failed,
                    "needs_sign_in": rediscovery.needs_sign_in,
                },
                status=status.HTTP_201_CREATED,
            )

        if not created:
            return Response(
                {"error": "User is already a member."},
                status=status.HTTP_409_CONFLICT,
            )
        # An earlier awaiting-access invite is now satisfied; left live, the next
        # login would "accept" it and send a second, contradictory email.
        now = timezone.now()
        WorkspaceInvite.objects.filter(
            workspace=workspace, email=email, status__in=LIVE_INVITE_STATUSES
        ).update(
            status=WorkspaceInviteStatus.ACCEPTED,
            resolved_at=now,
            resolved_membership=new_membership,
            updated_at=now,
        )
        # Defensive: the view runs in autocommit today, but inside an outer atomic
        # block a rollback must not email about a membership that never landed.
        transaction.on_commit(lambda: notify_member_added(new_membership, request.user))
        return Response(
            {
                "result": "member",
                "id": str(new_membership.id),
                "user_id": str(target.id),
                "email": target.email,
                "name": target.get_full_name(),
                "role": new_membership.role,
                "created_at": new_membership.created_at.isoformat(),
            },
            status=status.HTTP_201_CREATED,
        )


class WorkspaceMemberDetailView(APIView):
    """
    PATCH  /api/workspaces/<workspace_id>/members/<membership_id>/  — change role (manage only).
    DELETE /api/workspaces/<workspace_id>/members/<membership_id>/  — remove member (manage only).
    """

    permission_classes = [IsAuthenticated]

    def _get_target_membership(self, workspace, membership_id):
        try:
            return WorkspaceMembership.objects.get(id=membership_id, workspace=workspace)
        except WorkspaceMembership.DoesNotExist:
            return None

    def patch(self, request, workspace_id, membership_id):
        workspace, membership, err = resolve_workspace(
            request, workspace_id, require_coverage=False
        )
        if err:
            return err
        if membership.role != WorkspaceRole.MANAGE:
            return Response(
                {"error": "Only managers can change roles."}, status=status.HTTP_403_FORBIDDEN
            )

        target = self._get_target_membership(workspace, membership_id)
        if target is None:
            return Response({"error": "Member not found."}, status=status.HTTP_404_NOT_FOUND)

        new_role = request.data.get("role")
        if new_role not in WorkspaceRole.values:
            return Response({"error": "Invalid role."}, status=status.HTTP_400_BAD_REQUEST)

        # Without coverage the only role change is handing over: making another
        # member a manager, so a last manager who lost a source can then leave.
        handing_over = new_role == WorkspaceRole.MANAGE and target.user_id != request.user.id
        if not handing_over:
            _workspace, _membership, err = resolve_workspace(request, workspace_id)
            if err:
                return err

        # Prevent demoting the last manager
        if (
            target.role == WorkspaceRole.MANAGE
            and new_role != WorkspaceRole.MANAGE
            and _is_last_manager(workspace, target)
        ):
            return Response(
                {"error": "Cannot demote the last manager of the workspace."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if target.role != new_role:
            target.role = new_role
            target.save(update_fields=["role"])
            if target.user_id != request.user.id:
                transaction.on_commit(lambda: notify_role_changed(target, request.user))
        return Response({"id": str(target.id), "role": target.role})

    def delete(self, request, workspace_id, membership_id):
        workspace, membership, err = resolve_workspace(
            request, workspace_id, require_coverage=False
        )
        if err:
            return err

        target = self._get_target_membership(workspace, membership_id)
        if target is None:
            return Response({"error": "Member not found."}, status=status.HTTP_404_NOT_FOUND)

        # Allow self-removal; managers can remove others
        is_self = target.user_id == request.user.id
        if not is_self and membership.role != WorkspaceRole.MANAGE:
            return Response(
                {"error": "Only managers can remove other members."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not is_self:
            # Only leaving is exempt from coverage; acting on others still needs it.
            _workspace, _membership, err = resolve_workspace(request, workspace_id)
            if err:
                return err

        # Prevent removing the last manager
        if _is_last_manager(workspace, target):
            return Response(
                {"error": "Cannot remove the last manager of the workspace."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Delete the member's threads in this workspace
        removed_user = target.user
        Thread.objects.filter(workspace=workspace, user=removed_user).delete()

        target.delete()
        if not is_self:
            transaction.on_commit(
                lambda: notify_member_removed(workspace, removed_user, request.user)
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkspaceInviteDetailView(APIView):
    """
    PATCH  /api/workspaces/<workspace_id>/invites/<invite_id>/ — change invite role (manage only).
    DELETE /api/workspaces/<workspace_id>/invites/<invite_id>/ — revoke invite (manage only).
    """

    permission_classes = [IsAuthenticated]

    def _get_manager_context(self, request, workspace_id, invite_id):
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return None, None, err
        if membership.role != WorkspaceRole.MANAGE:
            return (
                None,
                None,
                Response(
                    {"error": "Only managers can manage invites."},
                    status=status.HTTP_403_FORBIDDEN,
                ),
            )
        try:
            invite = WorkspaceInvite.objects.get(id=invite_id, workspace=workspace)
        except WorkspaceInvite.DoesNotExist:
            return (
                None,
                None,
                Response({"error": "Invite not found."}, status=status.HTTP_404_NOT_FOUND),
            )
        return workspace, invite, None

    def patch(self, request, workspace_id, invite_id):
        _workspace, invite, err = self._get_manager_context(request, workspace_id, invite_id)
        if err:
            return err
        new_role = request.data.get("role")
        if new_role not in WorkspaceRole.values:
            return Response({"error": "Invalid role."}, status=status.HTTP_400_BAD_REQUEST)
        # Conditional: login may have accepted the invite since it was read (#561 G4).
        if not _update_if_live(invite, role=new_role, updated_at=timezone.now()):
            return _invite_no_longer_live()
        invite.role = new_role
        return Response(_serialize_invite(invite))

    def delete(self, request, workspace_id, invite_id):
        _workspace, invite, err = self._get_manager_context(request, workspace_id, invite_id)
        if err:
            return err
        # Conditional: revoking an invite login already accepted would hide a live
        # member behind a REVOKED row (#561 G4).
        revoked = _update_if_live(
            invite, status=WorkspaceInviteStatus.REVOKED, updated_at=timezone.now()
        )
        # Already REVOKED is the requested state (e.g. a retry after a dropped 204).
        if (
            not revoked
            and not WorkspaceInvite.objects.filter(
                pk=invite.pk, status=WorkspaceInviteStatus.REVOKED
            ).exists()
        ):
            return _invite_no_longer_live()
        # A retry of an already-revoked invite, or one that had lapsed, is no news.
        if revoked and not invite.is_expired:
            transaction.on_commit(lambda: notify_invite_revoked(invite, request.user))
        return Response(status=status.HTTP_204_NO_CONTENT)


def _awaiting_invite_message(invite, user) -> str:
    missing = missing_for_user(user, invite.workspace)
    if not missing:
        return (
            f"You were invited to '{invite.workspace.name}' and can now use all of its "
            "data sources. Sign in again to join."
        )
    return (
        f"You were invited to '{invite.workspace.name}', which needs access to every one "
        f"of its data sources. Still needed — {needed_text(missing_tenants_payload(missing))}. It unlocks "
        "automatically once you have them."
    )


class MyInvitesView(APIView):
    """GET /api/invites/ — the signed-in user's awaiting_access invites.

    Feeds the in-app 'you're invited but need upstream access' banner. Matched on
    the user's proven emails (same rule as the login resolver) so the message
    can't be surfaced against an unverified address.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        emails = proven_emails(request.user)

        invites = WorkspaceInvite.objects.filter(
            email__in=emails,
            status=WorkspaceInviteStatus.AWAITING_ACCESS,
            expires_at__gt=timezone.now(),
        ).select_related("workspace")
        return Response(
            [
                {
                    "id": str(i.id),
                    "workspace_name": i.workspace.name,
                    "message": _awaiting_invite_message(i, request.user),
                }
                for i in invites
            ]
        )


class WorkspaceTenantView(APIView):
    """
    POST   /api/workspaces/<workspace_id>/tenants/         — add tenant (manage only)
    DELETE /api/workspaces/<workspace_id>/tenants/<wt_id>/ — remove tenant (manage only);
        the last one deletes the workspace, see ``_delete_for_last_source``
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace, _membership, err = resolve_workspace(
            request, workspace_id, require_coverage=False
        )
        if err:
            return err

        tenants = []
        for wt in WorkspaceTenant.objects.filter(workspace=workspace).select_related("tenant"):
            tenants.append(
                {
                    "id": str(wt.id),
                    "tenant_id": str(wt.tenant.id),
                    "tenant_name": wt.tenant.canonical_name,
                    "provider": wt.tenant.provider,
                }
            )
        return Response(tenants)

    def post(self, request, workspace_id):
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        if membership.role != WorkspaceRole.MANAGE:
            return Response(
                {"error": "Only workspace managers can add tenants."},
                status=status.HTTP_403_FORBIDDEN,
            )

        tenant_id, err = string_field(request.data, "tenant_id")
        if err:
            return err
        if not tenant_id:
            return Response({"error": "tenant_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except (Tenant.DoesNotExist, ValidationError):
            return Response(
                {"error": "Tenant not found or not accessible."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate the requesting user can use this tenant (always, before idempotency check)
        requester_missing = requester_gaps(request.user, [tenant])
        if requester_missing and requester_missing[0].recovery == CoverageRecovery.CONNECT_SOURCE:
            # No relationship with this source at all: don't describe it to them.
            return Response(
                {"error": "You do not have access to this tenant."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if requester_missing:
            return Response(
                {
                    "error": (
                        "You can't add this source until you have usable access to it "
                        f"yourself — {remedy_text(requester_missing[0])}."
                    ),
                    "missing_tenants": missing_tenants_payload(requester_missing),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Refresh uncovered members with their own tokens before the locked recheck,
        # so someone granted access upstream since their last login is not refused.
        already_added = WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant).exists()
        lacking = [] if already_added else members_lacking_tenant(workspace, tenant)
        recheck = MemberRecheck()
        if lacking:
            # DRF APIView is sync; the refresh is async provider I/O.
            recheck = async_to_sync(_arefresh_members_for_provider)(
                [user for user, _missing in lacking], tenant.provider
            )
        try:
            wt, created = add_tenant_covered_by_members(workspace, tenant, actor_id=request.user.id)
        except MembersLackTenant as refused:
            return Response(
                _members_lack_source_body(tenant, refused.gaps, recheck),
                status=status.HTTP_409_CONFLICT,
            )
        if not created:
            return Response(
                {
                    "id": str(wt.id),
                    "tenant_id": str(tenant.id),
                    "tenant_name": tenant.canonical_name,
                },
                status=status.HTTP_200_OK,
            )
        return Response(
            {"id": str(wt.id), "tenant_id": str(tenant.id), "tenant_name": tenant.canonical_name},
            status=status.HTTP_202_ACCEPTED,
        )

    def delete(self, request, workspace_id, wt_id):
        workspace, membership, err = resolve_workspace(
            request, workspace_id, require_coverage=False
        )
        if err:
            return err
        if membership.role != WorkspaceRole.MANAGE:
            return Response(
                {"error": "Only workspace managers can remove tenants."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            wt = WorkspaceTenant.objects.get(id=wt_id, workspace=workspace)
        except WorkspaceTenant.DoesNotExist:
            return Response(
                {"error": "Tenant not found in workspace."}, status=status.HTTP_404_NOT_FOUND
            )

        # Without coverage a manager may only remove a source they are missing;
        # removing one they can still read would change the workspace for members
        # with full access. Removing the missing one is allowed even when shared:
        # it is how they regain access, and a covered manager could do the same,
        # unlike deleting the whole workspace.
        missing = {t.tenant_id for t in missing_tenants_for_member(request.user, workspace)}
        if missing and str(wt.tenant_id) not in missing:
            return Response(
                {
                    "error": "You can only remove a source you're missing until you can use them all."
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            remove_workspace_tenant(workspace, wt)
        except LastWorkspaceTenant:
            return _delete_for_last_source(request, workspace, wt)
        return Response(status=status.HTTP_204_NO_CONTENT)


LAST_SOURCE_PREFIX = "Removing the last data source deletes the workspace. "


def _delete_for_last_source(request, workspace, wt) -> Response:
    """Delete ``workspace`` because its last source is being removed (#381).

    A workspace never exists without a source, so this is a workspace delete and
    goes through the same refusals. It needs an explicit
    ``?confirm_delete_workspace=true``: without it the caller gets a 409 to show
    the manager what will be lost before anything is destroyed.
    """
    if refusal := _workspace_delete_refusal(request.user, workspace, last_source=True):
        refusal.data["error"] = LAST_SOURCE_PREFIX + refusal.data["error"]
        return refusal
    if request.query_params.get("confirm_delete_workspace") != "true":
        return Response(
            {
                "error": LAST_SOURCE_PREFIX
                + "Its conversations and data will be deleted for every member.",
                "requires_confirmation": "delete_workspace",
                "workspace_name": workspace.name,
                "member_count": workspace.memberships.count(),
            },
            status=status.HTTP_409_CONFLICT,
        )
    with transaction.atomic():
        # The last-source check's row locks ended with its transaction. Adding a
        # source takes this lock (add_tenant_covered_by_members), so re-checking
        # under it means no source can land between the check and the delete.
        if not Workspace.objects.select_for_update().filter(pk=workspace.pk).exists():
            # A concurrent confirmed delete (a double submit) already removed it.
            return Response({"workspace_deleted": True}, status=status.HTTP_200_OK)
        if workspace.workspace_tenants.exclude(id=wt.id).exists():
            try:
                remove_workspace_tenant(workspace, wt)
            except LastWorkspaceTenant:
                # Removal doesn't take this lock, so the other source can go too.
                pass
            else:
                return Response(status=status.HTTP_204_NO_CONTENT)
        workspace.delete()
    return Response({"workspace_deleted": True}, status=status.HTTP_200_OK)
