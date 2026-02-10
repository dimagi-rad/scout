"""
API views for data source management.
"""

import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .connectors.registry import get_connector
from .models import (
    DatabaseConnection,
    DataSource,
    DataSourceCredential,
    DataSourceType,
    MaterializedDataset,
    ProjectDataSource,
    SyncJob,
)
from .serializers import (
    DatabaseConnectionSerializer,
    DataSourceCredentialSerializer,
    DataSourceSerializer,
    DataSourceTypeSerializer,
    MaterializedDatasetSerializer,
    OAuthCallbackSerializer,
    OAuthStartSerializer,
    ProjectDataSourceSerializer,
    SyncJobSerializer,
)


class DatabaseConnectionViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing database connections.

    Only accessible by admin users.
    """

    queryset = DatabaseConnection.objects.all()
    serializer_class = DatabaseConnectionSerializer
    permission_classes = [IsAdminUser]

    @action(detail=True, methods=["post"])
    def test_connection(self, request: Request, pk=None) -> Response:
        """Test a database connection."""
        connection = self.get_object()

        try:
            import psycopg2

            conn_params = {
                "host": connection.db_host,
                "port": connection.db_port,
                "dbname": connection.db_name,
                "user": connection.db_user,
                "password": connection.db_password,
                "connect_timeout": 10,
            }

            conn = psycopg2.connect(**conn_params)
            cursor = conn.cursor()

            # Get list of schemas
            cursor.execute("""
                SELECT schema_name
                FROM information_schema.schemata
                WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                ORDER BY schema_name
            """)
            schemas = [row[0] for row in cursor.fetchall()]

            cursor.close()
            conn.close()

            return Response({
                "success": True,
                "schemas": schemas,
            })

        except Exception as e:
            return Response({
                "success": False,
                "error": str(e),
            })


class DataSourceViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing data sources.

    Only accessible by admin users.
    """

    queryset = DataSource.objects.all()
    serializer_class = DataSourceSerializer
    permission_classes = [IsAdminUser]


class ProjectDataSourceViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing project data sources.
    """

    queryset = ProjectDataSource.objects.select_related("data_source", "project")
    serializer_class = ProjectDataSourceSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Filter by project if specified."""
        queryset = super().get_queryset()
        project_id = self.request.query_params.get("project")
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        return queryset


class DataSourceCredentialViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for viewing data source credentials.

    Users can only see their own credentials.
    """

    serializer_class = DataSourceCredentialSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Return credentials for the current user."""
        return DataSourceCredential.objects.filter(
            user=self.request.user
        ).select_related("data_source")


class MaterializedDatasetViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for viewing materialized datasets.

    Users can see project-level datasets and their own user-level datasets.
    """

    serializer_class = MaterializedDatasetSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Return datasets visible to the current user."""
        from django.db.models import Q

        return MaterializedDataset.objects.filter(
            Q(user__isnull=True) | Q(user=self.request.user)
        ).select_related("project_data_source__data_source")

    @action(detail=True, methods=["post"])
    def trigger_sync(self, request: Request, pk=None) -> Response:
        """Manually trigger a sync for this dataset."""
        dataset = self.get_object()

        # Check if user owns this dataset or it's project-level
        if dataset.user and dataset.user != request.user:
            return Response(
                {"error": "You can only trigger syncs for your own datasets"},
                status=status.HTTP_403_FORBIDDEN,
            )

        from .tasks import sync_dataset

        sync_dataset.delay(str(dataset.id))

        return Response({"status": "sync_triggered"})


class SyncJobViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for viewing sync job history.
    """

    serializer_class = SyncJobSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Return sync jobs for datasets visible to the current user."""
        from django.db.models import Q

        return SyncJob.objects.filter(
            Q(materialized_dataset__user__isnull=True)
            | Q(materialized_dataset__user=self.request.user)
        ).select_related("materialized_dataset").order_by("-created_at")


class DataSourceTypesView(APIView):
    """List available data source types."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        types = [
            {"value": choice.value, "label": choice.label}
            for choice in DataSourceType
        ]
        serializer = DataSourceTypeSerializer(types, many=True)
        return Response(serializer.data)


class OAuthStartView(APIView):
    """
    Start the OAuth flow for a data source.

    Returns the authorization URL to redirect the user to.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        serializer = OAuthStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data_source_id = serializer.validated_data["data_source_id"]
        project_id = serializer.validated_data.get("project_id")

        try:
            data_source = DataSource.objects.get(id=data_source_id)
        except DataSource.DoesNotExist:
            return Response(
                {"error": "Data source not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Generate state token for CSRF protection
        state = secrets.token_urlsafe(32)

        # Store state in session
        request.session[f"oauth_state_{state}"] = {
            "data_source_id": str(data_source_id),
            "project_id": str(project_id) if project_id else None,
            "user_id": request.user.id,
        }

        # Get the connector and generate auth URL
        connector = get_connector(data_source)

        # Build redirect URI
        redirect_uri = request.build_absolute_uri("/api/datasources/oauth/callback/")

        # Get scopes from data source config
        scopes = data_source.config.get("scopes", [])

        auth_url = connector.get_oauth_authorization_url(
            redirect_uri=redirect_uri,
            state=state,
            scopes=scopes,
        )

        return Response({"authorization_url": auth_url})


class OAuthCallbackView(APIView):
    """
    Handle OAuth callback from the data source.

    Exchanges the authorization code for tokens and stores the credential.
    """

    permission_classes = []  # Public endpoint for OAuth redirect

    def get(self, request: Request) -> Response:
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        error = request.query_params.get("error")

        # Handle OAuth errors
        if error:
            error_description = request.query_params.get("error_description", error)
            return redirect(f"/datasources/connect?error={error_description}")

        if not code or not state:
            return redirect("/datasources/connect?error=Missing+code+or+state")

        # Validate state
        session_key = f"oauth_state_{state}"
        state_data = request.session.get(session_key)

        if not state_data:
            return redirect("/datasources/connect?error=Invalid+state")

        # Clean up session
        del request.session[session_key]

        try:
            data_source = DataSource.objects.get(id=state_data["data_source_id"])
        except DataSource.DoesNotExist:
            return redirect("/datasources/connect?error=Data+source+not+found")

        # Get the connector and exchange code for tokens
        connector = get_connector(data_source)
        redirect_uri = request.build_absolute_uri("/api/datasources/oauth/callback/")

        try:
            token_result = connector.exchange_code_for_tokens(
                code=code,
                redirect_uri=redirect_uri,
            )
        except Exception as e:
            return redirect(f"/datasources/connect?error={str(e)}")

        # Create or update credential
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user = User.objects.get(id=state_data["user_id"])

        project_id = state_data.get("project_id")

        # Determine if this is project-level or user-level
        credential_kwargs = {
            "data_source": data_source,
            "user": user,
        }
        if project_id:
            from apps.projects.models import Project

            credential_kwargs["project"] = Project.objects.get(id=project_id)
            credential_kwargs["user"] = None  # Project-level credential

        credential, created = DataSourceCredential.objects.update_or_create(
            **credential_kwargs,
            defaults={
                "is_valid": True,
            },
        )

        # Set encrypted tokens
        credential.access_token = token_result.access_token
        credential.refresh_token = token_result.refresh_token
        credential.token_expires_at = token_result.expires_at
        credential.save()

        # Redirect to success page
        return redirect("/datasources/connect?success=true")
