"""
API views and serializers for database connection management.
"""

from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAdminUser
from rest_framework.request import Request
from rest_framework.response import Response

from apps.projects.models import DatabaseConnection


class DatabaseConnectionSerializer(serializers.ModelSerializer):
    """Serializer for DatabaseConnection model."""

    # Write-only fields for credentials
    db_user = serializers.CharField(write_only=True, required=False)
    db_password = serializers.CharField(write_only=True, required=False)

    # Read-only computed fields
    project_count = serializers.SerializerMethodField()

    class Meta:
        model = DatabaseConnection
        fields = [
            "id",
            "name",
            "description",
            "db_host",
            "db_port",
            "db_name",
            "db_user",
            "db_password",
            "is_active",
            "project_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "project_count"]

    def get_project_count(self, obj: DatabaseConnection) -> int:
        return obj.projects.count()

    def create(self, validated_data: dict) -> DatabaseConnection:
        db_user = validated_data.pop("db_user", None)
        db_password = validated_data.pop("db_password", None)

        instance = DatabaseConnection(**validated_data)
        if db_user:
            instance.db_user = db_user
        if db_password:
            instance.db_password = db_password
        instance.save()
        return instance

    def update(self, instance: DatabaseConnection, validated_data: dict) -> DatabaseConnection:
        db_user = validated_data.pop("db_user", None)
        db_password = validated_data.pop("db_password", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if db_user:
            instance.db_user = db_user
        if db_password:
            instance.db_password = db_password

        instance.save()
        return instance


class DatabaseConnectionViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing database connections.

    Only accessible by admin users.
    """

    queryset = DatabaseConnection.objects.all()
    serializer_class = DatabaseConnectionSerializer
    permission_classes = [IsAdminUser]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

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
