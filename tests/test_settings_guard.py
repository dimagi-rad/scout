"""Guard that non-MCP entrypoints fail fast when DJANGO_SETTINGS_MODULE is unset.

Issue #248, finding 08#5: asgi.py / wsgi.py / manage.py used
``os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")``.
If the env var is dropped in a deployment, the process boots with the permissive
*development* settings (DEBUG=True, insecure cookies) instead of refusing to
start. The MCP entrypoint already fails fast; these tests pin the shared guard
the other entrypoints now use.

Pure-Python: no database and no Django settings load required.
"""

import importlib.util
from pathlib import Path

import pytest

# Load the guard module directly from its file so importing it does NOT trigger
# config/__init__.py (which imports the procrastinate app and needs Django).
_GUARD_PATH = Path(__file__).resolve().parent.parent / "config" / "settings_guard.py"
_spec = importlib.util.spec_from_file_location("config._settings_guard_under_test", _GUARD_PATH)
_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guard)
require_settings_module = _guard.require_settings_module


def test_returns_module_when_set():
    assert (
        require_settings_module({"DJANGO_SETTINGS_MODULE": "config.settings.production"})
        == "config.settings.production"
    )


def test_raises_when_missing():
    with pytest.raises(RuntimeError, match="DJANGO_SETTINGS_MODULE"):
        require_settings_module({})


def test_raises_when_empty():
    with pytest.raises(RuntimeError, match="DJANGO_SETTINGS_MODULE"):
        require_settings_module({"DJANGO_SETTINGS_MODULE": ""})
