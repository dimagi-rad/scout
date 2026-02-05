"""
Serializers for artifact sharing API.

Provides serializers for creating and listing shared artifact links.
"""
import secrets

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from apps.artifacts.models import AccessLevel, Artifact, SharedArtifact


class CreateShareSerializer(serializers.Serializer):
    """
    Serializer for creating a new share link for an artifact.

    Input fields:
        access_level: Who can access the shared artifact (public/project/specific)
        allowed_users: List of user IDs when access_level is 'specific'
        expires_at: Optional expiration datetime for the share link
    """

    access_level = serializers.ChoiceField(
        choices=AccessLevel.choices,
        default=AccessLevel.PROJECT,
        help_text="Access level for the share link.",
    )
    allowed_users = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=list,
        help_text="List of user IDs allowed to access when access_level is 'specific'.",
    )
    expires_at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="Optional expiration datetime for the share link.",
    )

    def validate_expires_at(self, value):
        """Ensure expiration date is in the future."""
        if value and value <= timezone.now():
            raise serializers.ValidationError("Expiration date must be in the future.")
        return value

    def validate(self, attrs):
        """Validate that allowed_users is provided when access_level is 'specific'."""
        access_level = attrs.get("access_level")
        allowed_users = attrs.get("allowed_users", [])

        if access_level == AccessLevel.SPECIFIC and not allowed_users:
            raise serializers.ValidationError({
                "allowed_users": "At least one user must be specified for 'specific' access level."
            })

        return attrs

    def create(self, validated_data):
        """Create a new SharedArtifact instance."""
        from apps.users.models import User

        artifact = self.context["artifact"]
        request = self.context["request"]
        allowed_user_ids = validated_data.pop("allowed_users", [])

        # Generate a unique token using secrets module for security
        share_token = secrets.token_urlsafe(32)

        shared_artifact = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=request.user,
            share_token=share_token,
            access_level=validated_data.get("access_level", AccessLevel.PROJECT),
            expires_at=validated_data.get("expires_at"),
        )

        # Add allowed users if specified
        if allowed_user_ids:
            users = User.objects.filter(pk__in=allowed_user_ids)
            shared_artifact.allowed_users.set(users)

        return shared_artifact


class SharedArtifactSerializer(serializers.ModelSerializer):
    """
    Serializer for representing a SharedArtifact in API responses.

    Includes the share URL, access level, expiration status, and view count.
    """

    share_url = serializers.CharField(read_only=True)
    is_expired = serializers.BooleanField(read_only=True)
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)
    allowed_user_emails = serializers.SerializerMethodField()

    class Meta:
        model = SharedArtifact
        fields = [
            "id",
            "share_token",
            "share_url",
            "access_level",
            "allowed_user_emails",
            "expires_at",
            "is_expired",
            "view_count",
            "created_by_email",
            "created_at",
        ]
        read_only_fields = fields

    def get_allowed_user_emails(self, obj):
        """Return list of allowed user emails for 'specific' access level."""
        if obj.access_level == AccessLevel.SPECIFIC:
            return list(obj.allowed_users.values_list("email", flat=True))
        return []


class SharedArtifactListSerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for listing shared artifacts.

    Used for the list endpoint to reduce payload size.
    """

    share_url = serializers.CharField(read_only=True)
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = SharedArtifact
        fields = [
            "id",
            "share_token",
            "share_url",
            "access_level",
            "expires_at",
            "is_expired",
            "view_count",
            "created_at",
        ]
        read_only_fields = fields
