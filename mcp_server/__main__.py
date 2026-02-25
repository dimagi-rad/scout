"""Entry point for `python -m mcp_server`."""

import os

if "DJANGO_SETTINGS_MODULE" not in os.environ:
    raise RuntimeError(
        "DJANGO_SETTINGS_MODULE environment variable is required. "
        "Set it to 'config.settings.development' or 'config.settings.production'."
    )

import django

django.setup()

from mcp_server.server import main  # noqa: E402

main()
