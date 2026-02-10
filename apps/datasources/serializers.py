"""
DRF serializers for data source models.
"""

from rest_framework import serializers

from .models import (
    DatabaseConnection,
    DataSource,
    DataSourceCredential,
    DataSourceType,
    MaterializedDataset,
    ProjectDataSource,
    SyncJob,
)


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


class DataSourceSerializer(serializers.ModelSerializer):
    """Serializer for DataSource model."""

    # Write-only for secrets
    oauth_client_secret = serializers.CharField(write_only=True, required=False)

    # Human-readable type
    source_type_display = serializers.CharField(source="get_source_type_display", read_only=True)

    class Meta:
        model = DataSource
        fields = [
            "id",
            "name",
            "source_type",
            "source_type_display",
            "base_url",
            "oauth_client_id",
            "oauth_client_secret",
            "config",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def create(self, validated_data: dict) -> DataSource:
        oauth_client_secret = validated_data.pop("oauth_client_secret", None)
        instance = DataSource(**validated_data)
        if oauth_client_secret:
            instance.oauth_client_secret = oauth_client_secret
        instance.save()
        return instance

    def update(self, instance: DataSource, validated_data: dict) -> DataSource:
        oauth_client_secret = validated_data.pop("oauth_client_secret", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if oauth_client_secret:
            instance.oauth_client_secret = oauth_client_secret

        instance.save()
        return instance


class ProjectDataSourceSerializer(serializers.ModelSerializer):
    """Serializer for ProjectDataSource model."""

    data_source_name = serializers.CharField(source="data_source.name", read_only=True)
    data_source_type = serializers.CharField(source="data_source.source_type", read_only=True)
    credential_mode_display = serializers.CharField(
        source="get_credential_mode_display", read_only=True
    )

    class Meta:
        model = ProjectDataSource
        fields = [
            "id",
            "project",
            "data_source",
            "data_source_name",
            "data_source_type",
            "credential_mode",
            "credential_mode_display",
            "sync_config",
            "refresh_interval_hours",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class DataSourceCredentialSerializer(serializers.ModelSerializer):
    """Serializer for DataSourceCredential - limited info for security."""

    data_source_name = serializers.CharField(source="data_source.name", read_only=True)
    data_source_type = serializers.CharField(source="data_source.source_type", read_only=True)

    class Meta:
        model = DataSourceCredential
        fields = [
            "id",
            "data_source",
            "data_source_name",
            "data_source_type",
            "project",
            "user",
            "token_expires_at",
            "is_valid",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "token_expires_at",
            "is_valid",
            "created_at",
            "updated_at",
        ]


class MaterializedDatasetSerializer(serializers.ModelSerializer):
    """Serializer for MaterializedDataset model."""

    data_source_name = serializers.CharField(
        source="project_data_source.data_source.name", read_only=True
    )
    data_source_type = serializers.CharField(
        source="project_data_source.data_source.source_type", read_only=True
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = MaterializedDataset
        fields = [
            "id",
            "project_data_source",
            "data_source_name",
            "data_source_type",
            "user",
            "schema_name",
            "status",
            "status_display",
            "last_sync_at",
            "last_activity_at",
            "row_counts",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "schema_name",
            "status",
            "last_sync_at",
            "last_activity_at",
            "row_counts",
            "created_at",
            "updated_at",
        ]


class SyncJobSerializer(serializers.ModelSerializer):
    """Serializer for SyncJob model."""

    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = SyncJob
        fields = [
            "id",
            "materialized_dataset",
            "status",
            "status_display",
            "started_at",
            "completed_at",
            "progress",
            "error_message",
            "resume_after",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "started_at",
            "completed_at",
            "progress",
            "error_message",
            "resume_after",
            "created_at",
        ]


class DataSourceTypeSerializer(serializers.Serializer):
    """Serializer for available data source types."""

    value = serializers.CharField()
    label = serializers.CharField()


class OAuthStartSerializer(serializers.Serializer):
    """Request serializer for starting OAuth flow."""

    data_source_id = serializers.UUIDField()
    project_id = serializers.UUIDField(required=False, allow_null=True)


class OAuthCallbackSerializer(serializers.Serializer):
    """Request serializer for OAuth callback."""

    code = serializers.CharField()
    state = serializers.CharField()
