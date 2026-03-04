from rest_framework import serializers

from apps.workspace.models import CustomWorkspace, CustomWorkspaceTenant, WorkspaceMembership


class CustomWorkspaceTenantSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(source="tenant_workspace.tenant_id", read_only=True)
    tenant_name = serializers.CharField(source="tenant_workspace.tenant_name", read_only=True)
    tenant_workspace_id = serializers.UUIDField(source="tenant_workspace.id", read_only=True)

    class Meta:
        model = CustomWorkspaceTenant
        fields = ["id", "tenant_workspace_id", "tenant_id", "tenant_name", "added_at"]


class WorkspaceMembershipSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    user_id = serializers.UUIDField(source="user.id", read_only=True)

    class Meta:
        model = WorkspaceMembership
        fields = ["id", "user_id", "email", "role", "joined_at"]


class CustomWorkspaceListSerializer(serializers.ModelSerializer):
    tenant_count = serializers.IntegerField(read_only=True)
    member_count = serializers.IntegerField(read_only=True)
    role = serializers.CharField(read_only=True)

    class Meta:
        model = CustomWorkspace
        fields = [
            "id",
            "name",
            "description",
            "created_at",
            "updated_at",
            "tenant_count",
            "member_count",
            "role",
        ]


class CustomWorkspaceDetailSerializer(serializers.ModelSerializer):
    tenants = CustomWorkspaceTenantSerializer(
        source="custom_workspace_tenants", many=True, read_only=True
    )
    members = WorkspaceMembershipSerializer(source="memberships", many=True, read_only=True)

    class Meta:
        model = CustomWorkspace
        fields = [
            "id",
            "name",
            "description",
            "system_prompt",
            "created_at",
            "updated_at",
            "tenants",
            "members",
        ]


class CustomWorkspaceCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, default="")
    tenant_workspace_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, default=list
    )
    tenant_ids = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )

    def validate(self, data):
        if not data.get("tenant_workspace_ids") and not data.get("tenant_ids"):
            raise serializers.ValidationError(
                "Either tenant_workspace_ids or tenant_ids is required."
            )
        return data
