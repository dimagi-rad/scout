from django.db import models
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.workspaces.models import WorkspaceRole

from .models import TransformationAsset, TransformationRun, TransformationScope
from .serializers import (
    LineageResponseSerializer,
    TransformationAssetSerializer,
    TransformationRunSerializer,
)


class TransformationAssetViewSet(viewsets.ModelViewSet):
    """CRUD for transformation assets with scope-based permissions.

    - System assets: read-only (403 on create/update/delete)
    - Tenant assets: any user with TenantMembership for that tenant
    - Workspace assets: users with read_write or manage role
    """

    serializer_class = TransformationAssetSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_ids = user.tenant_memberships.values_list("tenant_id", flat=True)
        workspace_ids = user.workspace_memberships.values_list("workspace_id", flat=True)

        qs = TransformationAsset.objects.filter(
            models.Q(tenant_id__in=tenant_ids) | models.Q(workspace_id__in=workspace_ids)
        )

        # Optional filters
        scope = self.request.query_params.get("scope")
        if scope:
            qs = qs.filter(scope=scope)
        tenant_id = self.request.query_params.get("tenant_id")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        workspace_id = self.request.query_params.get("workspace_id")
        if workspace_id:
            qs = qs.filter(workspace_id=workspace_id)

        return qs

    def perform_create(self, serializer):
        scope = serializer.validated_data.get("scope")
        if scope == TransformationScope.SYSTEM:
            raise PermissionDenied("System assets cannot be created via the API.")
        self._check_write_permission(serializer.validated_data)
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        if serializer.instance.scope == TransformationScope.SYSTEM:
            raise PermissionDenied("System assets cannot be modified.")
        instance = serializer.instance
        self._check_write_permission(
            {"workspace": instance.workspace, "tenant": instance.tenant}
        )
        serializer.save()

    def perform_destroy(self, instance):
        if instance.scope == TransformationScope.SYSTEM:
            raise PermissionDenied("System assets cannot be deleted.")
        self._check_write_permission(
            {"workspace": instance.workspace, "tenant": instance.tenant}
        )
        instance.delete()

    def _check_write_permission(self, data):
        """Verify the user has write access to the target container."""
        user = self.request.user
        if data.get("tenant"):
            is_member = user.tenant_memberships.filter(tenant=data["tenant"]).exists()
            if not is_member:
                raise PermissionDenied("You are not a member of this tenant.")
        if data.get("workspace"):
            has_write = user.workspace_memberships.filter(
                workspace=data["workspace"],
                role__in=[WorkspaceRole.READ_WRITE, WorkspaceRole.MANAGE],
            ).exists()
            if not has_write:
                raise PermissionDenied(
                    "You need read_write or manage role on this workspace."
                )

    @action(detail=True, methods=["get"])
    def lineage(self, request, pk=None):
        """GET /api/transformations/assets/{id}/lineage/"""
        from .services.lineage import get_lineage_chain

        asset = self.get_object()
        tenant_ids = list(request.user.tenant_memberships.values_list("tenant_id", flat=True))
        workspace_id = asset.workspace_id
        chain = get_lineage_chain(asset.name, tenant_ids, workspace_id)
        serializer = LineageResponseSerializer(chain, many=True)
        return Response(serializer.data)


class TransformationRunViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only access to transformation run history."""

    serializer_class = TransformationRunSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_ids = user.tenant_memberships.values_list("tenant_id", flat=True)
        workspace_ids = user.workspace_memberships.values_list("workspace_id", flat=True)

        qs = TransformationRun.objects.filter(
            models.Q(tenant_id__in=tenant_ids) | models.Q(workspace_id__in=workspace_ids)
        )

        tenant_id = self.request.query_params.get("tenant_id")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)

        return qs.prefetch_related("asset_runs__asset")

    @action(detail=False, methods=["post"])
    def trigger(self, request):
        """POST /api/transformations/runs/trigger/

        Body: {"tenant_id": "...", "workspace_id": "..." (optional)}
        Triggers a transformation run synchronously.
        """
        from apps.users.models import Tenant
        from apps.workspaces.models import TenantSchema

        from .services.executor import run_transformation_pipeline

        tenant_id = request.data.get("tenant_id")
        if not tenant_id:
            return Response({"error": "tenant_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except (Tenant.DoesNotExist, ValueError):
            return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        if not request.user.tenant_memberships.filter(tenant=tenant).exists():
            raise PermissionDenied("You are not a member of this tenant.")

        ts = TenantSchema.objects.filter(tenant=tenant, state="active").first()
        if not ts:
            return Response(
                {"error": "No active schema for this tenant"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        ts.touch()

        workspace = None
        workspace_id = request.data.get("workspace_id")
        if workspace_id:
            from apps.workspaces.models import Workspace

            workspace = Workspace.objects.filter(
                id=workspace_id,
                memberships__user=request.user,
                memberships__role__in=[WorkspaceRole.READ_WRITE, WorkspaceRole.MANAGE],
            ).first()
            if not workspace:
                raise PermissionDenied(
                    "Workspace not found or you are not a member."
                )

        run = run_transformation_pipeline(
            tenant=tenant,
            schema_name=ts.schema_name,
            workspace=workspace,
        )

        serializer = TransformationRunSerializer(run)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
