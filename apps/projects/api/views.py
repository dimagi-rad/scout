"""
API views for project management.

Provides endpoints for CRUD operations on projects, member management,
and database connection testing.
"""
import asyncio
import logging

from django.db.models import Count
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.projects.models import Project, ProjectMembership, ProjectRole
from apps.users.models import User

from .serializers import (
    AddMemberSerializer,
    ProjectDetailSerializer,
    ProjectListSerializer,
    ProjectMemberSerializer,
    TestConnectionSerializer,
)

logger = logging.getLogger(__name__)


class ProjectPermissionMixin:
    """
    Mixin providing permission checking for project operations.

    Provides methods to check if a user has access to a project
    and if they have admin permissions.
    """

    def get_project(self, project_id):
        """Retrieve a project by ID."""
        return get_object_or_404(
            Project.objects.prefetch_related("memberships"),
            pk=project_id,
        )

    def get_user_membership(self, user, project):
        """Get the user's membership in a project, if any."""
        if user.is_superuser:
            return None  # Superusers have implicit access
        return ProjectMembership.objects.filter(
            user=user,
            project=project,
        ).first()

    def check_project_access(self, request, project):
        """
        Check if the user has any access to the project.

        Returns:
            tuple: (has_access: bool, error_response: Response or None)
        """
        if request.user.is_superuser:
            return True, None

        membership = self.get_user_membership(request.user, project)
        if membership:
            return True, None

        return False, Response(
            {"error": "You do not have access to this project."},
            status=status.HTTP_403_FORBIDDEN,
        )

    def check_admin_permission(self, request, project):
        """
        Check if the user has admin permission for the project.

        Returns:
            tuple: (is_admin: bool, error_response: Response or None)
        """
        if request.user.is_superuser:
            return True, None

        membership = self.get_user_membership(request.user, project)
        if membership and membership.role == ProjectRole.ADMIN:
            return True, None

        return False, Response(
            {"error": "You must be a project admin to perform this action."},
            status=status.HTTP_403_FORBIDDEN,
        )


class ProjectListCreateView(APIView):
    """
    List all projects the user has access to, or create a new project.

    GET /api/projects/
        Returns list of projects where user is a member (or all for superusers).

    POST /api/projects/
        Creates a new project. The creator becomes an admin member.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        """List all projects accessible to the user."""
        if request.user.is_superuser:
            projects = Project.objects.all()
        else:
            project_ids = ProjectMembership.objects.filter(
                user=request.user
            ).values_list("project_id", flat=True)
            projects = Project.objects.filter(id__in=project_ids)

        projects = projects.prefetch_related("memberships").order_by("name")
        serializer = ProjectListSerializer(
            projects,
            many=True,
            context={"request": request},
        )
        return Response(serializer.data)

    def post(self, request):
        """Create a new project."""
        serializer = ProjectDetailSerializer(
            data=request.data,
            context={"request": request},
        )

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        project = serializer.save()

        # Return the created project
        response_serializer = ProjectDetailSerializer(
            project,
            context={"request": request},
        )
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class ProjectDetailView(ProjectPermissionMixin, APIView):
    """
    Retrieve, update, or delete a project.

    GET /api/projects/{project_id}/
        Returns project details. Requires membership.

    PUT /api/projects/{project_id}/
        Updates project. Requires admin role.

    DELETE /api/projects/{project_id}/
        Deletes project. Requires admin role.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        """Retrieve a project by ID."""
        project = self.get_project(project_id)

        has_access, error_response = self.check_project_access(request, project)
        if not has_access:
            return error_response

        serializer = ProjectDetailSerializer(
            project,
            context={"request": request},
        )
        return Response(serializer.data)

    def put(self, request, project_id):
        """Update a project."""
        project = self.get_project(project_id)

        is_admin, error_response = self.check_admin_permission(request, project)
        if not is_admin:
            return error_response

        serializer = ProjectDetailSerializer(
            project,
            data=request.data,
            partial=True,
            context={"request": request},
        )

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        serializer.save()
        return Response(serializer.data)

    def delete(self, request, project_id):
        """Delete a project."""
        project = self.get_project(project_id)

        is_admin, error_response = self.check_admin_permission(request, project)
        if not is_admin:
            return error_response

        project.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProjectMembersView(ProjectPermissionMixin, APIView):
    """
    List project members or add a new member.

    GET /api/projects/{project_id}/members/
        Returns list of project members. Requires membership.

    POST /api/projects/{project_id}/members/
        Adds a new member. Requires admin role.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        """List all members of a project."""
        project = self.get_project(project_id)

        has_access, error_response = self.check_project_access(request, project)
        if not has_access:
            return error_response

        memberships = ProjectMembership.objects.filter(
            project=project
        ).select_related("user").order_by("created_at")

        serializer = ProjectMemberSerializer(memberships, many=True)
        return Response(serializer.data)

    def post(self, request, project_id):
        """Add a new member to the project."""
        project = self.get_project(project_id)

        is_admin, error_response = self.check_admin_permission(request, project)
        if not is_admin:
            return error_response

        serializer = AddMemberSerializer(
            data=request.data,
            context={"project": project, "request": request},
        )

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        membership = serializer.save()

        # Return the created membership
        response_serializer = ProjectMemberSerializer(membership)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class ProjectMemberDetailView(ProjectPermissionMixin, APIView):
    """
    Remove a member from a project.

    DELETE /api/projects/{project_id}/members/{user_id}/
        Removes a member. Requires admin role.
        Admins cannot remove themselves if they are the last admin.
    """

    permission_classes = [IsAuthenticated]

    def delete(self, request, project_id, user_id):
        """Remove a member from the project."""
        project = self.get_project(project_id)

        is_admin, error_response = self.check_admin_permission(request, project)
        if not is_admin:
            return error_response

        # Get the target user
        target_user = get_object_or_404(User, pk=user_id)

        # Get the membership to delete
        membership = get_object_or_404(
            ProjectMembership,
            project=project,
            user=target_user,
        )

        # Prevent removing the last admin
        if membership.role == ProjectRole.ADMIN:
            admin_count = ProjectMembership.objects.filter(
                project=project,
                role=ProjectRole.ADMIN,
            ).count()
            if admin_count <= 1:
                return Response(
                    {"error": "Cannot remove the last admin from the project."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        membership.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TestConnectionView(APIView):
    """
    Test a database connection and return schema information.

    POST /api/projects/test-connection/
        Tests connection with provided credentials.
        Returns list of schemas and tables on success.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Test a database connection."""
        serializer = TestConnectionSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data

        try:
            result = asyncio.run(self._test_connection(
                host=data["db_host"],
                port=data["db_port"],
                database=data["db_name"],
                user=data["db_user"],
                password=data["db_password"],
                schema=data.get("db_schema", "public"),
            ))
            return Response(result)
        except Exception as e:
            logger.exception("Database connection test failed")
            return Response(
                {"error": f"Connection failed: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

    async def _test_connection(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        schema: str,
    ) -> dict:
        """
        Test the database connection and retrieve schema information.

        Returns:
            dict with 'success', 'schemas', and 'tables' keys.
        """
        import asyncpg

        conn = await asyncpg.connect(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
        )

        try:
            # Get list of schemas
            schemas = await conn.fetch("""
                SELECT schema_name
                FROM information_schema.schemata
                WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                ORDER BY schema_name
            """)

            # Get tables in the specified schema
            tables = await conn.fetch("""
                SELECT table_name, table_type
                FROM information_schema.tables
                WHERE table_schema = $1
                ORDER BY table_name
            """, schema)

            return {
                "success": True,
                "schemas": [row["schema_name"] for row in schemas],
                "tables": [
                    {
                        "name": row["table_name"],
                        "type": row["table_type"],
                    }
                    for row in tables
                ],
            }
        finally:
            await conn.close()
