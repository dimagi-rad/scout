"""
API views for artifact sharing functionality.

Provides endpoints for creating, listing, and revoking share links for artifacts.
"""
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.artifacts.models import Artifact, SharedArtifact
from apps.projects.models import ProjectMembership, ProjectRole

from .serializers import (
    CreateShareSerializer,
    SharedArtifactListSerializer,
    SharedArtifactSerializer,
)


class ArtifactSharePermissionMixin:
    """
    Mixin providing permission checking for artifact share operations.

    Only artifact creators or project admins can create/revoke share links.
    """

    def get_artifact(self, artifact_id):
        """Retrieve the artifact and check it exists."""
        return get_object_or_404(
            Artifact.objects.select_related("project", "created_by"),
            pk=artifact_id,
        )

    def check_share_permission(self, request, artifact):
        """
        Check if the user has permission to manage share links for this artifact.

        Returns:
            tuple: (has_permission: bool, error_response: Response or None)
        """
        # Superusers always have access
        if request.user.is_superuser:
            return True, None

        # Check if user is the artifact creator
        if artifact.created_by_id == request.user.id:
            return True, None

        # Check if user is a project admin
        membership = ProjectMembership.objects.filter(
            user=request.user,
            project=artifact.project,
        ).first()

        if membership and membership.role == ProjectRole.ADMIN:
            return True, None

        # No permission
        return False, Response(
            {"error": "You must be the artifact creator or a project admin to manage share links."},
            status=status.HTTP_403_FORBIDDEN,
        )


class CreateShareView(ArtifactSharePermissionMixin, APIView):
    """
    Create a new share link for an artifact.

    POST /api/artifacts/{artifact_id}/share/

    Request body:
        access_level: "public" | "project" | "specific" (default: "project")
        allowed_users: [user_id, ...] (required if access_level is "specific")
        expires_at: ISO datetime string (optional)

    Response:
        201 Created with SharedArtifactSerializer data
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, artifact_id):
        """Create a new share link for the artifact."""
        artifact = self.get_artifact(artifact_id)

        has_permission, error_response = self.check_share_permission(request, artifact)
        if not has_permission:
            return error_response

        serializer = CreateShareSerializer(
            data=request.data,
            context={"request": request, "artifact": artifact},
        )

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        shared_artifact = serializer.save()

        # Return the created share using the detail serializer
        response_serializer = SharedArtifactSerializer(shared_artifact)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class ListSharesView(ArtifactSharePermissionMixin, APIView):
    """
    List all share links for an artifact.

    GET /api/artifacts/{artifact_id}/shares/

    Response:
        200 OK with list of SharedArtifactListSerializer data
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, artifact_id):
        """List all share links for the artifact."""
        artifact = self.get_artifact(artifact_id)

        has_permission, error_response = self.check_share_permission(request, artifact)
        if not has_permission:
            return error_response

        shares = SharedArtifact.objects.filter(artifact=artifact).order_by("-created_at")
        serializer = SharedArtifactListSerializer(shares, many=True)

        return Response(serializer.data)


class RevokeShareView(ArtifactSharePermissionMixin, APIView):
    """
    Revoke (delete) a share link.

    DELETE /api/artifacts/{artifact_id}/shares/{share_token}/

    Response:
        204 No Content on success
        404 Not Found if share link doesn't exist
    """

    permission_classes = [IsAuthenticated]

    def delete(self, request, artifact_id, share_token):
        """Revoke a share link by deleting the SharedArtifact record."""
        artifact = self.get_artifact(artifact_id)

        has_permission, error_response = self.check_share_permission(request, artifact)
        if not has_permission:
            return error_response

        # Find and delete the share
        share = get_object_or_404(
            SharedArtifact,
            artifact=artifact,
            share_token=share_token,
        )

        share.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)
