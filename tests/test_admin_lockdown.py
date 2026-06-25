"""Tests for Django admin hardening (arch #260).

Covers:
- 11#3 — state-machine / identity fields readonly; SocialToken/SocialApp
  unregistered or hardened; UserAdmin privilege fields locked down.
- 11#5 — read-only operator-model registrations; RecipeAdmin surfaces
  Recipe.prompt and drops the dead RecipeStep scaffolding.
- 11#6 — AgentLearningAdmin.confidence_badge renders real HTML (format_html).
"""

import pytest
from allauth.socialaccount.models import SocialApp, SocialToken
from django.contrib import admin
from django.core.cache import cache
from django.utils.safestring import SafeString

from apps.chat.models import Thread, ThreadJob
from apps.knowledge.admin import AgentLearningAdmin
from apps.knowledge.models import AgentLearning
from apps.recipes.admin import RecipeAdmin
from apps.recipes.models import RecipeStep
from apps.transformations.models import TransformationRun
from apps.users.admin_login import ThrottledAdminAuthenticationForm
from apps.users.models import (
    Tenant,
    TenantConnection,
    TenantMembership,
    User,
)
from apps.users.rate_limiting import AUTH_MAX_ATTEMPTS
from apps.workspaces.models import (
    MaterializationRun,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceTenant,
    WorkspaceViewSchema,
)


class _FakeRequest:
    """Minimal request stand-in for ModelAdmin method calls."""

    def __init__(self, user=None):
        self.user = user


def _admin_for(model):
    return admin.site._registry[model]


# --- 11#3: dangerous state-machine / identity fields are readonly ---------- #


class TestStateMachineFieldsReadonly:
    def test_tenant_schema_state_fields_readonly(self):
        model_admin = _admin_for(TenantSchema)
        ro = set(model_admin.get_readonly_fields(_FakeRequest()))
        for field in ("schema_name", "state", "last_accessed_at", "tenant"):
            assert field in ro, f"TenantSchema.{field} must be readonly in admin"

    def test_materialization_run_state_fields_readonly(self):
        model_admin = _admin_for(MaterializationRun)
        ro = set(model_admin.get_readonly_fields(_FakeRequest()))
        for field in ("state", "result", "progress"):
            assert field in ro, f"MaterializationRun.{field} must be readonly in admin"

    def test_transformation_run_status_readonly(self):
        model_admin = _admin_for(TransformationRun)
        ro = set(model_admin.get_readonly_fields(_FakeRequest()))
        assert "status" in ro, "TransformationRun.status must be readonly in admin"


# --- 11#3: SocialToken / SocialApp not exposed in admin --------------------- #


class TestOAuthSecretsNotExposed:
    def test_social_token_admin_unregistered(self):
        assert SocialToken not in admin.site._registry, (
            "SocialTokenAdmin exposes plaintext OAuth tokens; it must be unregistered"
        )

    def test_social_app_admin_unregistered(self):
        assert SocialApp not in admin.site._registry, (
            "SocialAppAdmin exposes plaintext client secrets; it must be unregistered"
        )


# --- 11#3: UserAdmin privilege fields locked down -------------------------- #


class TestUserAdminPrivilegeLockdown:
    def test_privilege_fields_readonly(self):
        model_admin = _admin_for(User)
        ro = set(model_admin.get_readonly_fields(_FakeRequest()))
        for field in ("is_superuser", "user_permissions"):
            assert field in ro, f"UserAdmin.{field} must be readonly (self-escalation guard)"


# --- 11#5: operator models registered read-only --------------------------- #


OPERATOR_MODELS = [
    Workspace,
    WorkspaceTenant,
    WorkspaceMembership,
    WorkspaceViewSchema,
    ThreadJob,
    Thread,
    Tenant,
    TenantMembership,
    TenantConnection,
]


class TestOperatorModelsRegistered:
    @pytest.mark.parametrize("model", OPERATOR_MODELS)
    def test_model_registered(self, model):
        assert model in admin.site._registry, (
            f"{model.__name__} should have an admin registration for operator inspection"
        )

    @pytest.mark.parametrize("model", OPERATOR_MODELS)
    def test_model_admin_is_read_only(self, model):
        model_admin = _admin_for(model)
        req = _FakeRequest()
        assert model_admin.has_add_permission(req) is False, (
            f"{model.__name__} admin must not allow adds"
        )
        assert model_admin.has_change_permission(req) is False, (
            f"{model.__name__} admin must not allow changes"
        )
        assert model_admin.has_delete_permission(req) is False, (
            f"{model.__name__} admin must not allow deletes"
        )

    def test_tenant_connection_does_not_expose_encrypted_credential(self):
        model_admin = _admin_for(TenantConnection)
        # The encrypted credential must never be a list_display / editable field.
        assert "encrypted_credential" not in tuple(model_admin.list_display)


# --- 11#5: RecipeAdmin surfaces the live prompt, drops dead RecipeStep ----- #


class TestRecipeAdmin:
    def test_recipe_admin_surfaces_prompt(self):
        flat_fields = set()
        for _name, opts in RecipeAdmin.fieldsets:
            for field in opts.get("fields", ()):
                flat_fields.add(field)
        assert "prompt" in flat_fields, "RecipeAdmin must expose Recipe.prompt (the live field)"

    def test_recipe_admin_drops_recipestep_inline(self):
        assert not RecipeAdmin.inlines, "RecipeAdmin must not inline the dead RecipeStep model"

    def test_recipestep_admin_unregistered(self):
        assert RecipeStep not in admin.site._registry, (
            "RecipeStep is vestigial; its admin should be removed"
        )


# --- 11#6: confidence_badge renders real HTML ----------------------------- #


@pytest.mark.django_db
class TestConfidenceBadge:
    def test_confidence_badge_returns_safe_html(self):
        learning = AgentLearning(description="x", confidence_score=0.9)
        admin_obj = AgentLearningAdmin(AgentLearning, admin.site)
        result = admin_obj.confidence_badge(learning)
        assert isinstance(result, SafeString), (
            "confidence_badge must return format_html output, not a raw string"
        )
        assert "<span" in result
        # The literal escaped markup bug would render "&lt;span".
        assert "&lt;span" not in result

    def test_confidence_badge_has_no_allow_tags(self):
        assert not hasattr(AgentLearningAdmin.confidence_badge, "allow_tags"), (
            "allow_tags was removed in Django 2.0 and is a no-op on Django 5"
        )


# --- 11#3: admin login is throttled by the per-email limiter --------------- #


class TestAdminLoginThrottle:
    def test_admin_site_uses_throttled_login_form(self):
        assert admin.site.login_form is ThrottledAdminAuthenticationForm

    @pytest.mark.django_db
    def test_rate_limited_email_is_blocked(self):
        email = "victim@example.com"
        cache.set(f"auth_attempts:{email}", AUTH_MAX_ATTEMPTS, 300)
        try:
            form = ThrottledAdminAuthenticationForm(
                data={"username": email, "password": "whatever"}
            )
            assert not form.is_valid()
            assert "Too many login attempts" in str(form.errors)
        finally:
            cache.delete(f"auth_attempts:{email}")

    @pytest.mark.django_db
    def test_failed_login_records_attempt(self):
        email = "nobody@example.com"
        cache.delete(f"auth_attempts:{email}")
        try:
            form = ThrottledAdminAuthenticationForm(
                data={"username": email, "password": "wrong-password"}
            )
            assert not form.is_valid()  # no such user -> invalid login
            # The failed attempt must have been counted toward the limiter.
            assert cache.get(f"auth_attempts:{email}", 0) >= 1
        finally:
            cache.delete(f"auth_attempts:{email}")
