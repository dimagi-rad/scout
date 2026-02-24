from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.users"
    verbose_name = "Users"

    def ready(self):
        from allauth.socialaccount.signals import social_account_added, social_account_updated

        from apps.users.signals import resolve_tenant_on_social_login

        social_account_added.connect(resolve_tenant_on_social_login)
        social_account_updated.connect(resolve_tenant_on_social_login)
