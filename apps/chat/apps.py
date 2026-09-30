from django.apps import AppConfig
from django.core.checks import register

from .checks import check_checkpointer_shares_default_database


class ChatConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.chat"

    def ready(self):
        register(check_checkpointer_shares_default_database)
