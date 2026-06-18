#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys
from pathlib import Path

import environ

from config.settings_guard import require_settings_module


def main():
    """Run administrative tasks."""
    # Fail fast rather than defaulting to development settings (issue #248, 08#5).
    # Local dev configures DJANGO_SETTINGS_MODULE in .env (see .env.example), so
    # honor that file first — matching how config/settings/base.py reads it —
    # before requiring the variable to be set.
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.exists() and "DJANGO_SETTINGS_MODULE" not in os.environ:
        environ.Env.read_env(str(env_file))
    require_settings_module(os.environ)
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
