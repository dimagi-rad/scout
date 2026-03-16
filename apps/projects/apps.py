from django.apps import AppConfig


class ProjectsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.projects"
    verbose_name = "Projects"

    def ready(self):
        import logging

        from django.conf import settings

        cache_backend = settings.CACHES.get("default", {}).get("BACKEND", "")
        if "LocMemCache" in cache_backend:
            logging.getLogger("scout.config").warning(
                "REDIS_URL not set — using LocMemCache. "
                "Rate limiting and caching will not work across multiple workers."
            )
