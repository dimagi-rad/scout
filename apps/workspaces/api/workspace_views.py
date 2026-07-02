"""Workspace management API views."""

import asyncio
import logging

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import Count, OuterRef, Subquery
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.chat.models import Thread
from apps.users.models import Tenant, TenantMembership
from apps.users.services.credential_resolver import aget_fresh_access_token
from apps.users.services.tenant_resolution import (
    resolve_commcare_domains,
    resolve_connect_opportunities,
    resolve_ocs_chatbots,
)
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.workspace_resolver import resolve_workspace_drf as resolve_workspace

logger = logging.getLogger(__name__)

# Bounded so a slow upstream export can't tie up the sync DRF worker thread.
SHARE_REFRESH_TIMEOUT = 8  # seconds

_PROVIDER_RESOLVERS = {
    "commcare": resolve_commcare_domains,
    "commcare_connect": resolve_connect_opportunities,
    "ocs": resolve_ocs_chatbots,
}


async def _arefresh_target_for_workspace(target, providers) -> bool:
    """Best-effort, bounded server-side refresh of *target*'s memberships for the
    workspace's tenant providers, using the target's OWN (refresh-aware) token.

    This is what lets a manager add someone who was granted access upstream after
    the target's last Scout login — without the target manually reconnecting.
    Returns True if the target had a usable token for at least one provider (used
    to distinguish "no access upstream" from "needs to reconnect" in the error).
    """
    tried = False
    for provider in providers:
        resolve = _PROVIDER_RESOLVERS.get(provider)
        if resolve is None:
            continue
        token = await aget_fresh_access_token(target, provider)
        if not token:
            continue
        tried = True
        try:
            await asyncio.wait_for(resolve(target, token), timeout=SHARE_REFRESH_TIMEOUT)
        except Exception:
            logger.warning(
                "Share-time refresh failed for target=%s provider=%s",
                target.id,
                provider,
                exc_info=True,
            )
    return tried


def _is_last_manager(workspace, membership):
    """Return True if membership is the sole manager of workspace."""
    if membership.role != WorkspaceRole.MANAGE:
        return False
    return workspace.memberships.filter(role=WorkspaceRole.MANAGE).count() <= 1


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
        latest_run = (
            MaterializationRun.objects.filter(
                state=MaterializationRun.RunState.COMPLETED,
                tenant_schema__tenant__workspace_tenants__workspace=OuterRef("workspace"),
            )
            .order_by("-completed_at")
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
        )
        memberships = list(memberships)
        schema_statuses = _schema_status_for_workspaces([m.workspace for m in memberships])

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
            results.append(
                {
                    "id": str(m.workspace.id),
                    "name": m.workspace.name,
                    "display_name": m.workspace.display_name,
                    "is_auto_created": m.workspace.is_auto_created,
                    "role": m.role,
                    "tenants": tenants,
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

        workspace = Workspace.objects.create(
            name=name,
            is_auto_created=False,
            created_by=request.user,
        )
        tenants = []
        first_tenant = None
        for tenant in Tenant.objects.filter(id__in=tenant_ids):
            WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)
            if first_tenant is None:
                first_tenant = tenant
            tenants.append(
                {
                    "id": str(tenant.id),
                    "tenant_name": tenant.canonical_name,
                    "provider": tenant.provider,
                }
            )

        WorkspaceMembership.objects.create(
            workspace=workspace,
            user=request.user,
            role=WorkspaceRole.MANAGE,
        )

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
        workspace, membership, err = resolve_workspace(request, workspace_id)
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

        latest_completed = (
            MaterializationRun.objects.filter(
                state=MaterializationRun.RunState.COMPLETED,
                tenant_schema__tenant__in=tenants,
            )
            .order_by("-completed_at")
            .values_list("completed_at", flat=True)
            .first()
        )
        last_synced_at = latest_completed.isoformat() if latest_completed else None

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
                "system_prompt": workspace.system_prompt,
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
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        if membership.role != WorkspaceRole.MANAGE:
            return Response(
                {"error": "Only workspace managers can delete a workspace."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Check this is not the user's last workspace covering any tenant
        tenant_ids = list(workspace.workspace_tenants.values_list("tenant_id", flat=True))
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
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err

        memberships = WorkspaceMembership.objects.filter(workspace=workspace).select_related("user")
        results = [
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
        return Response(results)

    def post(self, request, workspace_id):
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        if membership.role != WorkspaceRole.MANAGE:
            return Response(
                {"error": "Only managers can add members."},
                status=status.HTTP_403_FORBIDDEN,
            )

        email = (request.data.get("email") or "").strip()
        if not email or "@" not in email:
            return Response({"error": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

        role = request.data.get("role")
        if role not in WorkspaceRole.values:
            return Response({"error": "Invalid role."}, status=status.HTTP_400_BAD_REQUEST)

        target = get_user_model().objects.filter(email__iexact=email).first()
        if target is None:
            return Response(
                {"error": "No Scout user with that email."},
                status=status.HTTP_404_NOT_FOUND,
            )

        workspace_tenant_ids = list(workspace.workspace_tenants.values_list("tenant_id", flat=True))

        def target_shares_tenant():
            return TenantMembership.objects.filter(
                user=target, tenant_id__in=workspace_tenant_ids
            ).exists()

        if not target_shares_tenant():
            # The target may have been granted access upstream (Connect/HQ/OCS)
            # after their last Scout login. Refresh their memberships server-side
            # using their own token, then re-check — no manual reconnect needed.
            providers = list(
                workspace.workspace_tenants.values_list("tenant__provider", flat=True).distinct()
            )
            had_token = async_to_sync(_arefresh_target_for_workspace)(target, providers)
            if not target_shares_tenant():
                if had_token:
                    msg = (
                        "This user doesn't have access to this workspace's data "
                        "source in the source system (e.g. the Connect opportunity)."
                    )
                else:
                    msg = (
                        "This user needs to sign into Scout again to refresh their "
                        "access (Connections in the left menu)."
                    )
                return Response({"error": msg}, status=status.HTTP_403_FORBIDDEN)

        if WorkspaceMembership.objects.filter(workspace=workspace, user=target).exists():
            return Response(
                {"error": "User is already a member."},
                status=status.HTTP_409_CONFLICT,
            )

        new_membership = WorkspaceMembership.objects.create(
            workspace=workspace,
            user=target,
            role=role,
            invited_by=request.user,
        )
        return Response(
            {
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
        workspace, membership, err = resolve_workspace(request, workspace_id)
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
        workspace, membership, err = resolve_workspace(request, workspace_id)
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


class WorkspaceTenantView(APIView):
    """
    POST   /api/workspaces/<workspace_id>/tenants/         — add tenant (manage only)
    DELETE /api/workspaces/<workspace_id>/tenants/<wt_id>/ — remove tenant (manage only)
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
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
        from apps.workspaces.services.workspace_service import add_workspace_tenant

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

        # Validate the requesting user has access to this tenant (always, before idempotency check)
        if not TenantMembership.objects.filter(user=request.user, tenant=tenant).exists():
            return Response(
                {"error": "You do not have access to this tenant."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        wt, created = add_workspace_tenant(workspace, tenant)
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
        from apps.workspaces.services.workspace_service import remove_workspace_tenant

        workspace, membership, err = resolve_workspace(request, workspace_id)
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

        try:
            remove_workspace_tenant(workspace, wt)
        except ValidationError as e:
            return Response({"error": e.message}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)
