"""
ASGI config for Scout data agent platform.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.0/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

from config.settings_guard import require_settings_module

# Fail fast rather than defaulting to development settings (issue #248, 08#5).
require_settings_module(os.environ)

application = get_asgi_application()
