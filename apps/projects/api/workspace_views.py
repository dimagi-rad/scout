"""Workspace management API views."""

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.projects.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from apps.projects.workspace_resolver import resolve_workspace


class WorkspaceListView(APIView):
    """
    GET  /api/workspaces/  — list workspaces the authenticated user is a member of.
    POST /api/workspaces/  — create a new workspace.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        memberships = WorkspaceMembership.objects.filter(user=request.user).select_related(
            "workspace"
        )
        results = []
        for m in memberships:
            ws = m.workspace
            results.append(
                {
                    "id": str(ws.id),
                    "name": ws.name,
                    "is_auto_created": ws.is_auto_created,
                    "role": m.role,
                    "tenant_count": ws.workspace_tenants.count(),
                    "member_count": ws.memberships.count(),
                    "created_at": ws.created_at.isoformat(),
                }
            )
        return Response(results)

    def post(self, request):
        name = request.data.get("name", "").strip()
        if not name:
            return Response({"error": "name is required."}, status=status.HTTP_400_BAD_REQUEST)

        tenant_ids = request.data.get("tenant_ids", [])
        if not tenant_ids:
            return Response(
                {"error": "At least one tenant_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.users.models import TenantMembership

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

        from apps.users.models import Tenant

        workspace = Workspace.objects.create(
            name=name,
            is_auto_created=False,
            created_by=request.user,
        )
        for tid in tenant_ids:
            tenant = Tenant.objects.get(id=tid)
            WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)

        WorkspaceMembership.objects.create(
            workspace=workspace,
            user=request.user,
            role=WorkspaceRole.MANAGE,
        )

        return Response(
            {
                "id": str(workspace.id),
                "name": workspace.name,
                "is_auto_created": workspace.is_auto_created,
                "role": WorkspaceRole.MANAGE,
                "tenant_count": len(tenant_ids),
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

        from apps.projects.models import SchemaState, TenantSchema

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

        return Response(
            {
                "id": str(workspace.id),
                "name": workspace.name,
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
            workspace.system_prompt = system_prompt

        workspace.save(update_fields=["name", "system_prompt", "updated_at"])
        return Response({"id": str(workspace.id), "name": workspace.name})

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
            return Response({"error": "Only managers can change roles."}, status=403)

        target = self._get_target_membership(workspace, membership_id)
        if target is None:
            return Response({"error": "Member not found."}, status=404)

        new_role = request.data.get("role")
        if new_role not in WorkspaceRole.values:
            return Response({"error": "Invalid role."}, status=400)

        # Prevent demoting the last manager
        if target.role == WorkspaceRole.MANAGE and new_role != WorkspaceRole.MANAGE:
            manage_count = workspace.memberships.filter(role=WorkspaceRole.MANAGE).count()
            if manage_count <= 1:
                return Response(
                    {"error": "Cannot demote the last manager of the workspace."},
                    status=400,
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
            return Response({"error": "Member not found."}, status=404)

        # Allow self-removal; managers can remove others
        is_self = target.user_id == request.user.id
        if not is_self and membership.role != WorkspaceRole.MANAGE:
            return Response({"error": "Only managers can remove other members."}, status=403)

        # Prevent removing the last manager
        if target.role == WorkspaceRole.MANAGE:
            manage_count = workspace.memberships.filter(role=WorkspaceRole.MANAGE).count()
            if manage_count <= 1:
                return Response(
                    {"error": "Cannot remove the last manager of the workspace."},
                    status=400,
                )

        # Delete the member's threads in this workspace
        from apps.chat.models import Thread

        Thread.objects.filter(workspace=workspace, user=target.user).delete()

        target.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
