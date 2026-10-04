from django.apps import AppConfig

from apps.users.token_encryption import install_socialtoken_encryption


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.users"
    verbose_name = "Users"

    def ready(self):
        install_socialtoken_encryption(self.apps.get_model("socialaccount", "SocialToken"))

        # Signal modules import models, which aren't loadable until the app registry is ready.
        from allauth.account.signals import user_signed_up  # noqa: PLC0415
        from allauth.socialaccount.signals import (  # noqa: PLC0415
            pre_social_login,
            social_account_added,
        )

        import apps.users.signals  # noqa: F401, PLC0415 — connects auto_create_workspace_on_membership
        from apps.users.signals import (  # noqa: PLC0415
            reconcile_existing_user_on_login,
            resolve_existing_tenants_on_social_login,
            resolve_tenant_on_social_login,
            resolve_tenant_on_social_signup,
        )

        social_account_added.connect(resolve_tenant_on_social_login)
        user_signed_up.connect(resolve_tenant_on_social_signup)
        pre_social_login.connect(reconcile_existing_user_on_login)
        pre_social_login.connect(resolve_existing_tenants_on_social_login)
