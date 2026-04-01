from rest_framework import serializers

from .models import TransformationAsset, TransformationAssetRun, TransformationRun


class TransformationAssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = TransformationAsset
        fields = [
            "id",
            "name",
            "description",
            "scope",
            "tenant",
            "workspace",
            "sql_content",
            "replaces",
            "test_yaml",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Scope replaces FK to assets visible to the requesting user
        if "replaces" in self.fields and "request" in self.context:
            user = self.context["request"].user
            tenant_ids = user.tenant_memberships.values_list("tenant_id", flat=True)
            workspace_ids = user.workspace_memberships.values_list("workspace_id", flat=True)
            from django.db import models

            self.fields["replaces"].queryset = TransformationAsset.objects.filter(
                models.Q(tenant_id__in=tenant_ids) | models.Q(workspace_id__in=workspace_ids)
            )

    def get_extra_kwargs(self):
        """Make scope, tenant, and workspace immutable after creation."""
        extra_kwargs = super().get_extra_kwargs()
        if self.instance is not None:
            for field_name in ("scope", "tenant", "workspace"):
                kwargs = extra_kwargs.get(field_name, {})
                kwargs["read_only"] = True
                extra_kwargs[field_name] = kwargs
        return extra_kwargs


class TransformationAssetRunSerializer(serializers.ModelSerializer):
    asset_name = serializers.CharField(source="asset.name", read_only=True)

    class Meta:
        model = TransformationAssetRun
        fields = [
            "id",
            "asset",
            "asset_name",
            "status",
            "duration_ms",
            "logs",
            "test_results",
            "started_at",
            "completed_at",
        ]
        read_only_fields = fields


class TransformationRunSerializer(serializers.ModelSerializer):
    asset_runs = TransformationAssetRunSerializer(many=True, read_only=True)

    class Meta:
        model = TransformationRun
        fields = [
            "id",
            "tenant",
            "workspace",
            "status",
            "started_at",
            "completed_at",
            "error_message",
            "asset_runs",
        ]
        read_only_fields = fields


class LineageResponseSerializer(serializers.Serializer):
    """Read-only serializer for lineage chain entries."""

    name = serializers.CharField()
    scope = serializers.CharField()
    description = serializers.CharField()
