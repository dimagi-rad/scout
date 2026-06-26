"""
Admin configuration for User model and identity-related models.

Hardening (arch #260, 11#3):
- UserAdmin makes the privilege-escalation fields (is_superuser,
  user_permissions, groups) readonly so staff with users.change_user cannot
  self-escalate to superuser through the change form.
- allauth's auto-registered SocialToken / SocialApp admins are unregistered:
  they expose every user's plaintext OAuth access/refresh tokens and the
  platform's OAuth client secrets behind the (weakly throttled) admin login.
- Operator identity models (Tenant, TenantMembership, TenantConnection) are
  registered read-only for incident inspection (11#5); TenantConnection never
  surfaces its encrypted credential.
"""

import contextlib

from allauth.socialaccount.models import SocialApp, SocialToken
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from apps.common.admin import ReadOnlyModelAdmin
from apps.users.admin_login import ThrottledAdminAuthenticationForm

from .models import Tenant, TenantConnection, TenantMembership, User

# Route /admin/login/ through Scout's per-email auth rate limiter (11#3), so an
# admin brute force is throttled identically to /api/auth/.
admin.site.login_form = ThrottledAdminAuthenticationForm

# allauth auto-registers these in its AppConfig.ready(); unregister so plaintext
# tokens / client secrets are never browsable through the admin. Guarded so a
# future allauth that stops auto-registering doesn't raise NotRegistered.
for _model in (SocialToken, SocialApp):
    with contextlib.suppress(admin.sites.NotRegistered):
        admin.site.unregister(_model)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """Admin interface for User model."""

    list_display = ["email", "first_name", "last_name", "is_staff", "is_active", "created_at"]
    list_filter = ["is_staff", "is_active", "created_at"]
    search_fields = ["email", "first_name", "last_name"]
    ordering = ["email"]

    # is_superuser / user_permissions / groups grant privileges; making them
    # readonly closes the self-escalation path (staff with change_user can no
    # longer promote themselves or anyone else to superuser via the admin).
    readonly_fields = ["is_superuser", "user_permissions", "groups", "last_login", "date_joined"]

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal Info", {"fields": ("first_name", "last_name", "avatar_url", "timezone")}),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2"),
            },
        ),
    )


@admin.register(Tenant)
class TenantAdmin(ReadOnlyModelAdmin):
    list_display = ["canonical_name", "provider", "external_id", "created_at"]
    list_filter = ["provider"]
    search_fields = ["canonical_name", "external_id"]


@admin.register(TenantMembership)
class TenantMembershipAdmin(ReadOnlyModelAdmin):
    list_display = ["user", "tenant", "connection", "last_selected_at", "archived_at"]
    list_filter = ["tenant__provider"]
    search_fields = ["user__email", "tenant__canonical_name", "tenant__external_id"]


@admin.register(TenantConnection)
class TenantConnectionAdmin(ReadOnlyModelAdmin):
    # Deliberately excludes ``encrypted_credential`` from list_display so the
    # ciphertext is not casually surfaced in listings.
    list_display = ["user", "provider", "credential_type", "created_at"]
    list_filter = ["provider", "credential_type"]
    search_fields = ["user__email"]
