"""Workspace management API views."""

from django.core.exceptions import ValidationError
from django.db.models import Count
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.workspace_resolver import resolve_workspace_drf as resolve_workspace


def _is_last_manager(workspace, membership):
    """Return True if membership is the sole manager of workspace."""
    if membership.role != WorkspaceRole.MANAGE:
        return False
    return workspace.memberships.filter(role=WorkspaceRole.MANAGE).count() <= 1


class WorkspaceListView(APIView):
    """
    GET  /api/workspaces/  — list workspaces the authenticated user is a member of.
    POST /api/workspaces/  — create a new workspace.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        memberships = (
            WorkspaceMembership.objects.filter(user=request.user)
            .select_related("workspace")
            .prefetch_related("workspace__workspace_tenants__tenant")
            .annotate(
                member_count=Count("workspace__memberships", distinct=True),
            )
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
            results.append(
                {
                    "id": str(m.workspace.id),
                    "name": m.workspace.name,
                    "display_name": m.workspace.display_name,
                    "is_auto_created": m.workspace.is_auto_created,
                    "role": m.role,
                    "tenants": tenants,
                    "member_count": m.member_count,
                    "created_at": m.workspace.created_at.isoformat(),
                }
            )
        return Response(results)

    def post(self, request):
        name = request.data.get("name", "").strip()
        if not name:
            return Response({"error": "name is required."}, status=status.HTTP_400_BAD_REQUEST)

        tenant_ids = request.data.get("tenant_ids", [])

        # Validate user has access to all requested tenants
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

        if active_schemas == len(tenants) and len(tenants) > 0:
            schema_status = "available"
        elif provisioning:
            schema_status = "provisioning"
        else:
            schema_status = "unavailable"

        # Multi-tenant workspaces track readiness via WorkspaceViewSchema
        if len(tenants) > 1:
            try:
                vs = workspace.view_schema
                schema_status = "available" if vs.state == SchemaState.ACTIVE else "provisioning"
            except WorkspaceViewSchema.DoesNotExist:
                schema_status = "provisioning"

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
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace, membership, err = resolve_workspace(request, workspace_id)
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
        if target.role == WorkspaceRole.MANAGE and new_role != WorkspaceRole.MANAGE:
            if _is_last_manager(workspace, target):
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
        from apps.chat.models import (
            Thread,  # noqa: PLC0415 — avoids circular import at module level
        )

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
        workspace, membership, err = resolve_workspace(request, workspace_id)
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
