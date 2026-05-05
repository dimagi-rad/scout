from django.apps import AppConfig


class WorkspacesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.workspaces"
    verbose_name = "Workspaces"

    def ready(self):
        import logging

        from django.conf import settings

        cache_backend = settings.CACHES.get("default", {}).get("BACKEND", "")
        if "LocMemCache" in cache_backend:
            logging.getLogger("scout.config").warning(
                "Using LocMemCache. "
                "Rate limiting and caching will not work across multiple workers."
            )
