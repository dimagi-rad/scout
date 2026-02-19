"""
Admin configuration for Project models.
"""
from django.contrib import admin
from django.utils.html import format_html

from .models import DatabaseConnection, Project, ProjectMembership


@admin.register(DatabaseConnection)
class DatabaseConnectionAdmin(admin.ModelAdmin):
    list_display = ["name", "db_host", "db_name", "is_active", "created_by", "created_at"]
    list_filter = ["is_active", "created_at"]
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
            {"fields": ["is_active", "created_by", "created_at", "updated_at"]},
        ),
    ]


class ProjectMembershipInline(admin.TabularInline):
    """Inline admin for project memberships."""

    model = ProjectMembership
    extra = 1
    autocomplete_fields = ["user"]


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    """Admin interface for Project model."""

    list_display = [
        "name",
        "slug",
        "database_connection",
        "db_schema",
        "member_count",
        "has_data_dictionary",
        "created_at",
    ]
    list_filter = ["created_at", "llm_model"]
    search_fields = ["name", "slug", "description"]
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = [
        "id",
        "created_at",
        "updated_at",
        "data_dictionary_generated_at",
        "data_dictionary_display",
    ]
    autocomplete_fields = ["created_by", "database_connection"]
    inlines = [ProjectMembershipInline]
    actions = ["regenerate_data_dictionary"]

    fieldsets = (
        (None, {"fields": ("id", "name", "slug", "description")}),
        (
            "Database Connection",
            {
                "fields": (
                    "database_connection",
                    "db_schema",
                ),
            },
        ),
        (
            "Table Access Control",
            {
                "fields": ("allowed_tables", "excluded_tables"),
                "description": "Control which tables the agent can access. Empty allowed_tables means all tables.",
            },
        ),
        (
            "Agent Configuration",
            {
                "fields": (
                    "system_prompt",
                    "max_rows_per_query",
                    "max_query_timeout_seconds",
                    "llm_model",
                ),
            },
        ),
        (
            "Data Dictionary",
            {
                "fields": (
                    "data_dictionary_display",
                    "data_dictionary_generated_at",
                ),
                "description": "Run 'python manage.py generate_data_dictionary --project-slug <slug>' to regenerate.",
            },
        ),
        (
            "Metadata",
            {
                "fields": ("created_by", "created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description="Members")
    def member_count(self, obj):
        return obj.memberships.count()

    @admin.display(description="Data Dictionary", boolean=True)
    def has_data_dictionary(self, obj):
        return obj.data_dictionary is not None

    @admin.display(description="Data Dictionary Status")
    def data_dictionary_display(self, obj):
        if obj.data_dictionary:
            table_count = len(obj.data_dictionary.get("tables", {}))
            return format_html(
                '<span style="color: green;">{}</span> ({} tables)',
                "Generated",
                table_count,
            )
        return format_html(
            '<span style="color: orange;">{}</span>',
            "Not generated",
        )

    @admin.action(description="Regenerate data dictionary for selected projects")
    def regenerate_data_dictionary(self, request, queryset):
        """Admin action to regenerate data dictionaries."""
        from django.contrib import messages

        from .services.data_dictionary import DataDictionaryGenerator

        success_count = 0
        error_count = 0

        for project in queryset:
            try:
                generator = DataDictionaryGenerator(project)
                generator.generate()
                success_count += 1
            except Exception as e:
                error_count += 1
                self.message_user(
                    request,
                    f"Error generating dictionary for {project.name}: {e}",
                    messages.ERROR,
                )

        if success_count:
            self.message_user(
                request,
                f"Successfully regenerated data dictionary for {success_count} project(s).",
                messages.SUCCESS,
            )


@admin.register(ProjectMembership)
class ProjectMembershipAdmin(admin.ModelAdmin):
    """Admin interface for ProjectMembership model."""

    list_display = ["user", "project", "role", "created_at"]
    list_filter = ["role", "created_at", "project"]
    search_fields = ["user__email", "project__name"]
    autocomplete_fields = ["user", "project"]
