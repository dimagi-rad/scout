from django.apps import AppConfig

from apps.users.token_encryption import install_socialtoken_encryption


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.users"
    verbose_name = "Users"

    def ready(self):
        install_socialtoken_encryption(self.apps.get_model("socialaccount", "SocialToken"))

        from allauth.socialaccount.signals import (
            pre_social_login,
            social_account_added,
        )

        import apps.users.signals  # noqa: F401 — connects auto_create_workspace_on_membership
        from apps.users.signals import (
            reconcile_existing_user_on_login,
            resolve_existing_tenants_on_social_login,
            resolve_tenant_on_social_login,
        )

        social_account_added.connect(resolve_tenant_on_social_login)
        pre_social_login.connect(reconcile_existing_user_on_login)
        pre_social_login.connect(resolve_existing_tenants_on_social_login)
