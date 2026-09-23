from django.apps import AppConfig
from django.core.checks import Tags, register

from .checks import check_cube_configuration


class SemanticConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.semantic"
    verbose_name = "Semantic Model"

    def ready(self):
        register(Tags.compatibility)(check_cube_configuration)
