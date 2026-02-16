"""
Serializers for projects API.

Provides serializers for project CRUD operations and member management.
"""
from rest_framework import serializers

from apps.projects.models import Project, ProjectMembership, ProjectRole
from apps.users.models import User


class ProjectListSerializer(serializers.ModelSerializer):
    """
    Serializer for listing projects.

    Includes basic project info and the requesting user's role.
    """

    role = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "role",
            "member_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_role(self, obj):
        """Get the requesting user's role in this project."""
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None

        if request.user.is_superuser:
            return ProjectRole.ADMIN

        membership = obj.memberships.filter(user=request.user).first()
        return membership.role if membership else None

    def get_member_count(self, obj):
        """Get the number of members in this project."""
        return obj.memberships.count()


class ProjectDetailSerializer(serializers.ModelSerializer):
    """
    Serializer for project create/update operations.

    Uses a database_connection FK (UUID) instead of inline credentials.
    """

    database_connection_name = serializers.CharField(
        source="database_connection.name",
        read_only=True,
    )
    member_count = serializers.SerializerMethodField(read_only=True)
    role = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Project
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "database_connection",
            "database_connection_name",
            "db_schema",
            "allowed_tables",
            "excluded_tables",
            "system_prompt",
            "max_rows_per_query",
            "max_query_timeout_seconds",
            "llm_model",
            "data_dictionary",
            "data_dictionary_generated_at",
            "readonly_role",
            "is_active",
            "member_count",
            "role",
            "created_at",
            "updated_at",
            "created_by",
        ]
        read_only_fields = [
            "id",
            "data_dictionary",
            "data_dictionary_generated_at",
            "readonly_role",
            "member_count",
            "role",
            "created_at",
            "updated_at",
            "created_by",
        ]

    def get_member_count(self, obj):
        """Get the number of members in this project."""
        return obj.memberships.count()

    def get_role(self, obj):
        """Get the requesting user's role in this project."""
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None

        if request.user.is_superuser:
            return ProjectRole.ADMIN

        membership = obj.memberships.filter(user=request.user).first()
        return membership.role if membership else None

    def create(self, validated_data):
        """Create a new project with the requesting user as admin."""
        request = self.context.get("request")

        if request and request.user.is_authenticated:
            validated_data["created_by"] = request.user

        project = Project.objects.create(**validated_data)

        # Add the creator as an admin member
        if request and request.user.is_authenticated:
            ProjectMembership.objects.create(
                user=request.user,
                project=project,
                role=ProjectRole.ADMIN,
            )

        return project


class ProjectMemberSerializer(serializers.ModelSerializer):
    """
    Serializer for listing project members.

    Includes user details and their role in the project.
    """

    id = serializers.UUIDField(source="user.id", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    name = serializers.SerializerMethodField()

    class Meta:
        model = ProjectMembership
        fields = [
            "id",
            "email",
            "name",
            "role",
            "created_at",
        ]
        read_only_fields = fields

    def get_name(self, obj):
        """Get the user's full name or email as fallback."""
        return obj.user.get_full_name() or obj.user.email


class AddMemberSerializer(serializers.Serializer):
    """
    Serializer for adding a member to a project.

    Validates that the user exists and is not already a member.
    """

    email = serializers.EmailField(
        help_text="Email address of the user to add.",
    )
    role = serializers.ChoiceField(
        choices=ProjectRole.choices,
        default=ProjectRole.VIEWER,
        help_text="Role to assign to the new member.",
    )

    def validate_email(self, value):
        """Validate that a user with this email exists."""
        try:
            user = User.objects.get(email=value)
            self._user = user
        except User.DoesNotExist:
            raise serializers.ValidationError(
                f"No user found with email: {value}"
            )
        return value

    def validate(self, attrs):
        """Validate that the user is not already a member."""
        project = self.context.get("project")
        if project and hasattr(self, "_user"):
            if ProjectMembership.objects.filter(
                project=project, user=self._user
            ).exists():
                raise serializers.ValidationError({
                    "email": "This user is already a member of the project."
                })
        return attrs

    def create(self, validated_data):
        """Create a new project membership."""
        project = self.context["project"]
        membership = ProjectMembership.objects.create(
            project=project,
            user=self._user,
            role=validated_data["role"],
        )
        return membership
