"""Workspace management API views."""

import asyncio
import logging

from allauth.account.models import EmailAddress
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, OuterRef, Subquery
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.chat.models import Thread
from apps.users.models import Tenant, TenantMembership
from apps.users.services.credential_resolver import (
    aiter_fresh_access_tokens,
    aiter_social_tokens,
)
from apps.users.services.tenant_resolution import (
    resolve_commcare_domains,
    resolve_connect_opportunities,
    resolve_ocs_chatbots,
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
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceInvite,
    WorkspaceInviteStatus,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
    default_invite_expiry,
)
from apps.workspaces.services.credential_coverage import CoverageRecovery
from apps.workspaces.services.invite_notifications import (
    notify_awaiting_access,
    send_pending_invite_email,
)
from apps.workspaces.services.member_coverage import (
    MembersLackTenant,
    add_tenant_covered_by_members,
    admit_covered_member,
    members_lacking_tenant,
    missing_for_user,
    requester_gaps,
)
from apps.workspaces.services.workspace_service import remove_workspace_tenant
from apps.workspaces.workspace_resolver import resolve_workspace_drf as resolve_workspace

logger = logging.getLogger(__name__)

# Bounded so a slow upstream export can't tie up the sync DRF worker thread.
SHARE_REFRESH_TIMEOUT = 8  # seconds

# A PARTIAL run loaded some sources but not all — the data it wrote is present
# and queryable, so it counts as a sync. Excluding it made a workspace whose runs
# are perpetually PARTIAL report last_synced_at=null alongside
# schema_status="available", i.e. "never synced" about data the agent can query.
# Every other read of the "latest data-bearing run" already uses this pair (e.g.
# mcp_server/services/metadata.py, apps/workspaces/api/views.py).
SYNCED_RUN_STATES = (
    MaterializationRun.RunState.COMPLETED,
    MaterializationRun.RunState.PARTIAL,
)

_PROVIDER_RESOLVERS = {
    "commcare": resolve_commcare_domains,
    "commcare_connect": resolve_connect_opportunities,
    "ocs": resolve_ocs_chatbots,
}


async def _aunexpired_access_tokens(user, provider) -> list[tuple]:
    """``(identity, access token)`` pairs usable as-is, without renewing any.

    For refreshes a *different* user triggers: renewing someone else's token can
    record a refresh failure on a transient provider error, which would take away
    access they currently have.
    """
    now = timezone.now()
    return [
        (token.account, token.token)
        for token in await aiter_social_tokens(user, provider)
        if token.token and token.expires_at is not None and token.expires_at > now
    ]


async def _arefresh_target_for_workspace(target, providers, *, renew_tokens=True) -> bool:
    """Best-effort, bounded server-side refresh of *target*'s memberships for the
    workspace's tenant providers, using the target's OWN (refresh-aware) token.

    This is what lets a manager add someone who was granted access upstream after
    the target's last Scout login — without the target manually reconnecting.
    Returns True if the target had a usable token for at least one provider (used
    to distinguish "no access upstream" from "needs to reconnect" in the error).

    Every identity per provider is refreshed. A target holding two OCS teams has
    a token per team, and refreshing only one would report them as not covering a
    tenant they can in fact reach — the false negative multi-token OAuth exists
    to remove (#156).
    """
    tried = False
    for provider in providers:
        resolve = _PROVIDER_RESOLVERS.get(provider)
        if resolve is None:
            continue
        tokens = (
            await aiter_fresh_access_tokens(target, provider)
            if renew_tokens
            else await _aunexpired_access_tokens(target, provider)
        )
        for account, token in tokens:
            tried = True
            try:
                await asyncio.wait_for(
                    resolve(target, token, social_account=account, allow_replace=False),
                    timeout=SHARE_REFRESH_TIMEOUT,
                )
            except Exception:
                logger.warning(
                    "Share-time refresh failed for target=%s provider=%s account=%s",
                    target.id,
                    provider,
                    account.pk,
                    exc_info=True,
                )
    return tried


# Bounds the provider fan-out and keeps queued refreshes from spending their
# timeout waiting on the (serialized) persistence legs of the ones ahead.
MEMBER_REFRESH_CONCURRENCY = 4
# Whole fan-out, so a large workspace can't hold a sync worker indefinitely.
MEMBER_REFRESH_BUDGET = 2 * SHARE_REFRESH_TIMEOUT


async def _arefresh_members_for_provider(users, provider) -> None:
    """Best-effort rediscovery with each user's own, still-valid identities.

    Advisory only: the locked coverage check decides, so one member's failure
    must neither fail the request nor stop the others. Tokens are not renewed:
    a manager's click must never mark another member's credential as failed.
    """
    gate = asyncio.Semaphore(MEMBER_REFRESH_CONCURRENCY)

    async def refresh(user):
        async with gate:
            await _arefresh_target_for_workspace(user, [provider], renew_tokens=False)

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
    for user, task in zip(users, tasks, strict=True):
        result = task.exception() if task.done() and not task.cancelled() else None
        if isinstance(result, Exception):
            logger.warning(
                "Source-add refresh failed for member=%s provider=%s",
                user.id,
                provider,
                exc_info=result,
            )


def _member_label(user) -> str:
    name = user.get_full_name()
    return f"{name} <{user.email}>" if name else user.email


def _members_lack_source_body(tenant, gaps) -> dict:
    names = ", ".join(_member_label(user) for user, _missing in gaps)
    return {
        "error": (
            f"Can't add '{tenant.canonical_name}': {names} can't use it with their own "
            "account yet, and every member must be able to use every source. They can "
            "connect it in Connected Accounts once they have access to it; otherwise "
            "remove them from this workspace or create a separate workspace for this source."
        ),
        "reason": "members_lack_source",
        "members": [
            {
                "user_id": str(user.id),
                "email": user.email,
                "name": user.get_full_name(),
                **missing_tenants_payload([missing])[0],
            }
            for user, missing in gaps
        ],
    }


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


def _upsert_invite(workspace, email, role, invited_by, new_status):
    """Create or refresh the single live invite for (workspace, email).

    Re-inviting an outstanding invite is idempotent — it updates role/expiry and
    the pending↔awaiting_access status rather than violating the
    one-live-invite-per-(workspace,email) constraint. A stale (expired) live
    invite is retired to EXPIRED first so a fresh one can take its place.
    """
    live = WorkspaceInvite.objects.filter(
        workspace=workspace, email=email, status__in=LIVE_INVITE_STATUSES
    ).first()
    if live and not live.is_expired:
        live.role = role
        live.invited_by = invited_by
        live.status = new_status
        live.expires_at = default_invite_expiry()
        live.save(update_fields=["role", "invited_by", "status", "expires_at", "updated_at"])
        return live
    if live and live.is_expired:
        live.status = WorkspaceInviteStatus.EXPIRED
        live.save(update_fields=["status", "updated_at"])
    return WorkspaceInvite.objects.create(
        workspace=workspace,
        email=email,
        role=role,
        invited_by=invited_by,
        status=new_status,
    )


def _derive_schema_status(tenant_count, active_count, provisioning, view_schema_state):
    """Derive a workspace's schema status, shared by the list and detail endpoints
    so they never drift. Returns "available" | "provisioning" | "unavailable" | "failed".

    - Single-tenant: available iff every tenant is ACTIVE; provisioning if any is
      mid-provisioning; else unavailable.
    - Multi-tenant: tracked by the view schema — ACTIVE ⇒ available, FAILED ⇒
      failed (per-tenant data may have loaded but there's no queryable surface),
      else provisioning.
    """
    if tenant_count > 1:
        if view_schema_state == SchemaState.ACTIVE:
            return "available"
        if view_schema_state == SchemaState.FAILED:
            return "failed"
        return "provisioning"

    if active_count == tenant_count and tenant_count > 0:
        return "available"
    if provisioning:
        return "provisioning"
    return "unavailable"


def _schema_status_for_workspaces(workspaces):
    """Compute schema_status for many workspaces with bulk queries (no N+1).

    ``workspaces`` must have ``workspace_tenants__tenant`` prefetched. Returns a
    dict mapping workspace id -> status string.
    """
    workspace_ids = [w.id for w in workspaces]
    if not workspace_ids:
        return {}

    # All tenant ids across these workspaces.
    tenant_ids = {wt.tenant_id for w in workspaces for wt in w.workspace_tenants.all()}

    # Per-tenant schema states (one bulk query).
    active_tenants = set()
    provisioning_tenants = set()
    if tenant_ids:
        for tenant_id, state in TenantSchema.objects.filter(tenant_id__in=tenant_ids).values_list(
            "tenant_id", "state"
        ):
            if state == SchemaState.ACTIVE:
                active_tenants.add(tenant_id)
            elif state in (SchemaState.PROVISIONING, SchemaState.MATERIALIZING):
                provisioning_tenants.add(tenant_id)

    # Multi-tenant workspaces' view schema states (one bulk query).
    view_states = dict(
        WorkspaceViewSchema.objects.filter(workspace_id__in=workspace_ids).values_list(
            "workspace_id", "state"
        )
    )

    statuses = {}
    for w in workspaces:
        ws_tenant_ids = [wt.tenant_id for wt in w.workspace_tenants.all()]
        active_count = sum(1 for tid in ws_tenant_ids if tid in active_tenants)
        provisioning = any(tid in provisioning_tenants for tid in ws_tenant_ids)
        statuses[w.id] = _derive_schema_status(
            tenant_count=len(ws_tenant_ids),
            active_count=active_count,
            provisioning=provisioning,
            view_schema_state=view_states.get(w.id),
        )
    return statuses


class WorkspaceListView(APIView):
    """
    GET  /api/workspaces/  — list workspaces the authenticated user is a member of.
    POST /api/workspaces/  — create a new workspace.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        # completed_at__isnull=False because Postgres sorts NULLs first under
        # DESC — one state-bearing run without a timestamp would otherwise shadow
        # every real one and return null.
        latest_run = (
            MaterializationRun.objects.filter(
                state__in=SYNCED_RUN_STATES,
                completed_at__isnull=False,
                tenant_schema__tenant__workspace_tenants__workspace=OuterRef("workspace"),
            )
            .order_by("-completed_at")
            .values("completed_at")[:1]
        )

        memberships = (
            WorkspaceMembership.objects.filter(user=request.user)
            .select_related("workspace")
            # display_name uses tenants.first(); Tenant's default ordering keeps it cached.
            .prefetch_related("workspace__workspace_tenants__tenant", "workspace__tenants")
            .annotate(
                member_count=Count("workspace__memberships", distinct=True),
                last_synced_at=Subquery(latest_run),
            )
            .order_by("-workspace__created_at", "-workspace_id")
        )
        memberships = list(memberships)
        schema_statuses = _schema_status_for_workspaces([m.workspace for m in memberships])

        # Surfaced per row (rather than filtering rows out) so the client can keep
        # denied workspaces addressable by URL while gating them in the UI and
        # telling the member which sources to connect.
        missing_by_ws = missing_tenants_by_workspace(
            request.user, [m.workspace for m in memberships]
        )

        results = []
        for m in memberships:
            tenants = [
                {
                    "id": str(wt.tenant.id),
                    "tenant_name": wt.tenant.canonical_name,
                    "provider": wt.tenant.provider,
                }
                for wt in m.workspace.workspace_tenants.all()
            ]
            missing = missing_by_ws[m.workspace.id]
            results.append(
                {
                    "id": str(m.workspace.id),
                    "name": m.workspace.name,
                    "display_name": m.workspace.display_name,
                    "is_auto_created": m.workspace.is_auto_created,
                    "role": m.role,
                    "tenants": tenants,
                    "has_access": not missing,
                    "missing_tenants": missing_tenants_payload(missing),
                    "member_count": m.member_count,
                    "schema_status": schema_statuses.get(m.workspace.id, "unavailable"),
                    "last_synced_at": (m.last_synced_at.isoformat() if m.last_synced_at else None),
                    "created_at": m.workspace.created_at.isoformat(),
                }
            )
        return Response(results)

    def post(self, request):
        name = request.data.get("name", "").strip()
        if not name:
            return Response({"error": "name is required."}, status=status.HTTP_400_BAD_REQUEST)

        tenant_ids = request.data.get("tenant_ids", [])

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
        tenants = [
            {
                "id": str(tenant.id),
                "tenant_name": tenant.canonical_name,
                "provider": tenant.provider,
            }
            for tenant in selected
        ]
        first_tenant = selected[0] if selected else None

        display_name = (
            first_tenant.format_display_name(workspace.name) if first_tenant else workspace.name
        )
        return Response(
            {
                "id": str(workspace.id),
                "name": workspace.name,
                "display_name": display_name,
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

        tenants = list(workspace.tenants.all())
        active_schemas = TenantSchema.objects.filter(
            tenant__in=tenants, state=SchemaState.ACTIVE
        ).count()
        provisioning = TenantSchema.objects.filter(
            tenant__in=tenants,
            state__in=[SchemaState.PROVISIONING, SchemaState.MATERIALIZING],
        ).exists()

        view_schema_state = None
        if len(tenants) > 1:
            try:
                view_schema_state = workspace.view_schema.state
            except WorkspaceViewSchema.DoesNotExist:
                view_schema_state = None

        schema_status = _derive_schema_status(
            tenant_count=len(tenants),
            active_count=active_schemas,
            provisioning=provisioning,
            view_schema_state=view_schema_state,
        )

        last_run_at = (
            MaterializationRun.objects.filter(
                state__in=SYNCED_RUN_STATES,
                completed_at__isnull=False,
                tenant_schema__tenant__in=tenants,
            )
            .order_by("-completed_at")
            .values_list("completed_at", flat=True)
            .first()
        )
        last_synced_at = last_run_at.isoformat() if last_run_at else None

        first_tenant = tenants[0] if tenants else None
        display_name = (
            first_tenant.format_display_name(workspace.name) if first_tenant else workspace.name
        )
        return Response(
            {
                "id": str(workspace.id),
                "name": workspace.name,
                "display_name": display_name,
                "is_auto_created": workspace.is_auto_created,
                "role": membership.role,
                # Agent configuration is workspace content, not page metadata.
                "system_prompt": workspace.system_prompt
                if not missing_tenants_for_member(request.user, workspace)
                else "",
                "schema_status": schema_status,
                "tenant_count": len(tenants),
                "member_count": workspace.memberships.count(),
                "created_at": workspace.created_at.isoformat(),
                "updated_at": workspace.updated_at.isoformat(),
                "last_synced_at": last_synced_at,
            }
        )

    def patch(self, request, workspace_id):
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        if membership.role != WorkspaceRole.MANAGE:
            return Response(
                {"error": "Only workspace managers can rename a workspace."},
                status=status.HTTP_403_FORBIDDEN,
            )

        name = request.data.get("name", "").strip()
        if name:
            workspace.name = name
        system_prompt = request.data.get("system_prompt")
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
        # Deleting destroys every member's content, so without coverage it is a
        # remediation only for a workspace nobody else is in.
        missing = {t.tenant_id for t in missing_tenants_for_member(request.user, workspace)}
        if missing and workspace.memberships.exclude(user=request.user).exists():
            return Response(
                {
                    "error": "You can't delete a shared workspace while you're missing one "
                    "of its sources. Remove that source, or make another member a "
                    "manager and leave."
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
            other_workspaces = Workspace.objects.filter(
                workspace_tenants__tenant_id=tid,
                memberships__user=request.user,
            ).exclude(id=workspace.id)
            if not other_workspaces.exists():
                return Response(
                    {
                        "error": "Cannot delete your last workspace covering a tenant. "
                        "Create another workspace for that tenant first."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        workspace.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkspaceMemberListView(APIView):
    """
    GET  /api/workspaces/<workspace_id>/members/  — list members (any member).
    POST /api/workspaces/<workspace_id>/members/  — add an existing user as a member (manage only).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        # The only source of the membership id that leaving needs, so reachable
        # without coverage; but then it shows only the caller's own row.
        workspace, _membership, err = resolve_workspace(
            request, workspace_id, require_coverage=False
        )
        if err:
            return err
        covered = not missing_tenants_for_member(request.user, workspace)

        memberships = WorkspaceMembership.objects.filter(workspace=workspace).select_related("user")
        if not covered:
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

        email = (request.data.get("email") or "").strip().lower()
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
        if gaps:
            # The target may have been granted access upstream (Connect/HQ/OCS)
            # after their last Scout login. Refresh their memberships server-side
            # using their own token, then re-check — no manual reconnect needed.
            providers = sorted({t.provider for t in gaps})
            async_to_sync(_arefresh_target_for_workspace)(target, providers)

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
            # The invitee already has a Scout account, so they never get the
            # pending-invite email; tell them directly they need upstream access.
            # The manager just performed this action, so don't email them.
            notify_awaiting_access(invite, target, notify_manager=False)
            return Response(
                _serialize_invite(invite, result="invite_awaiting_access"),
                status=status.HTTP_201_CREATED,
            )

        if not created:
            return Response(
                {"error": "User is already a member."},
                status=status.HTTP_409_CONFLICT,
            )
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

        target.role = new_role
        target.save(update_fields=["role"])
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
        Thread.objects.filter(workspace=workspace, user=target.user).delete()

        target.delete()
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
        invite.role = new_role
        invite.save(update_fields=["role", "updated_at"])
        return Response(_serialize_invite(invite))

    def delete(self, request, workspace_id, invite_id):
        _workspace, invite, err = self._get_manager_context(request, workspace_id, invite_id)
        if err:
            return err
        invite.status = WorkspaceInviteStatus.REVOKED
        invite.save(update_fields=["status", "updated_at"])
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
    the user's VERIFIED emails (same rule as the login resolver) so the message
    can't be surfaced against an unverified address.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        emails = {
            e.lower()
            for e in EmailAddress.objects.filter(user=request.user, verified=True).values_list(
                "email", flat=True
            )
        }
        if request.user.email:
            emails.add(request.user.email.lower())

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
    DELETE /api/workspaces/<workspace_id>/tenants/<wt_id>/ — remove tenant (manage only)
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

        tenant_id = request.data.get("tenant_id")
        if not tenant_id:
            return Response({"error": "tenant_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
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
        if lacking:
            async_to_sync(_arefresh_members_for_provider)(
                [user for user, _missing in lacking], tenant.provider
            )
        try:
            wt, created = add_tenant_covered_by_members(workspace, tenant)
        except MembersLackTenant as refused:
            return Response(
                _members_lack_source_body(tenant, refused.gaps), status=status.HTTP_409_CONFLICT
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
        # removing one they can't read either would change the workspace for
        # members who still have full access.
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
        except ValidationError as e:
            return Response({"error": e.message}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)
