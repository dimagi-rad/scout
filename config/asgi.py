"""
ASGI config for Scout data agent platform.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.0/howto/deployment/asgi/
"""

import asyncio
import os
import sys

# On Windows the default ProactorEventLoop is incompatible with psycopg's
# async driver.  Switch to SelectorEventLoop so the LangGraph PostgreSQL
# checkpointer (and any other async psycopg usage) works correctly.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

application = get_asgi_application()
