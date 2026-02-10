from django.contrib import admin

from .models import (
    DatabaseConnection,
    DataSource,
    DataSourceCredential,
    MaterializedDataset,
    ProjectDataSource,
    SyncJob,
)


@admin.register(DatabaseConnection)
class DatabaseConnectionAdmin(admin.ModelAdmin):
    list_display = ["name", "db_host", "db_name", "created_by", "created_at"]
    list_filter = ["created_at"]
    search_fields = ["name", "db_host", "db_name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    exclude = ["_db_user", "_db_password"]

    fieldsets = [
        (None, {"fields": ["id", "name", "description"]}),
        (
            "Connection",
            {"fields": ["db_host", "db_port", "db_name"]},
        ),
        (
            "Metadata",
            {"fields": ["created_by", "created_at", "updated_at"]},
        ),
    ]


@admin.register(DataSource)
class DataSourceAdmin(admin.ModelAdmin):
    list_display = ["name", "source_type", "base_url", "created_at"]
    list_filter = ["source_type", "created_at"]
    search_fields = ["name", "base_url"]
    readonly_fields = ["id", "created_at", "updated_at"]
    exclude = ["_oauth_client_secret"]

    fieldsets = [
        (None, {"fields": ["id", "name", "source_type"]}),
        (
            "API Configuration",
            {"fields": ["base_url", "config", "oauth_client_id"]},
        ),
        (
            "Metadata",
            {"fields": ["created_by", "created_at", "updated_at"]},
        ),
    ]


@admin.register(ProjectDataSource)
class ProjectDataSourceAdmin(admin.ModelAdmin):
    list_display = ["project", "data_source", "credential_mode", "is_active", "created_at"]
    list_filter = ["credential_mode", "is_active", "data_source__source_type"]
    search_fields = ["project__name", "data_source__name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    raw_id_fields = ["project", "data_source"]


@admin.register(DataSourceCredential)
class DataSourceCredentialAdmin(admin.ModelAdmin):
    list_display = ["data_source", "get_owner", "is_valid", "token_expires_at", "last_used_at"]
    list_filter = ["is_valid", "data_source__source_type"]
    search_fields = ["data_source__name", "project__name", "user__email"]
    readonly_fields = ["id", "created_at", "updated_at"]
    raw_id_fields = ["data_source", "project", "user"]
    exclude = ["_access_token", "_refresh_token"]

    @admin.display(description="Owner")
    def get_owner(self, obj):
        return obj.project.name if obj.project else obj.user.email


@admin.register(MaterializedDataset)
class MaterializedDatasetAdmin(admin.ModelAdmin):
    list_display = [
        "project_data_source",
        "user",
        "schema_name",
        "status",
        "last_sync_at",
        "last_activity_at",
    ]
    list_filter = ["status", "project_data_source__data_source__source_type"]
    search_fields = ["schema_name", "project_data_source__project__name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    raw_id_fields = ["project_data_source", "user"]


@admin.register(SyncJob)
class SyncJobAdmin(admin.ModelAdmin):
    list_display = [
        "materialized_dataset",
        "status",
        "started_at",
        "completed_at",
        "created_at",
    ]
    list_filter = ["status"]
    search_fields = ["materialized_dataset__schema_name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    raw_id_fields = ["materialized_dataset"]
