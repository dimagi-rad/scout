"""Load config.settings.production inside a test session booted under test settings."""

import importlib
import sys

PRODUCTION_SETTINGS = "config.settings.production"


def load_production_settings():
    """Import production settings afresh so the module body (and its startup checks)
    runs against the current environment."""
    sys.modules.pop(PRODUCTION_SETTINGS, None)
    return importlib.import_module(PRODUCTION_SETTINGS)
