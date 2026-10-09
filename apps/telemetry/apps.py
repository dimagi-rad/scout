from django.apps import AppConfig


class TelemetryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.telemetry"
    verbose_name = "Telemetry"

    def ready(self):
        # Signal modules import models, which aren't loadable until the app registry is ready.
        import apps.telemetry.signals  # noqa: F401, PLC0415
