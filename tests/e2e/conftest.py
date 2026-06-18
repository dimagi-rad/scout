"""E2E test configuration for live semantic-layer tests.

These tests hit the real dev DB (agent_platform on localhost:5435) and the
live Cube API — they do NOT use a test database.  The ``django_db_setup``
fixture is overridden to skip test DB creation so pytest-django allows ORM
calls against the existing dev database.

``allow_database_queries`` is also session-scoped so that async ORM calls
inside ``async_to_sync`` worker threads (which lose the per-context DB guard)
can still access the dev database.

Run:
    CUBE_E2E=1 DJANGO_SETTINGS_MODULE=config.settings.development \\
        uv run pytest tests/e2e -m cube_e2e -v
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def django_db_setup():
    """Skip test database creation — e2e tests use the real dev DB."""
    pass


@pytest.fixture(autouse=True, scope="session")
def allow_database_queries(django_db_blocker):
    """Globally unblock Django DB access for the entire e2e session.

    pytest-django blocks DB access by default.  These tests hit the live dev
    DB (not a test DB), so we unblock once at session scope.  The
    ``django_db_setup`` override above ensures no test DB is created or torn
    down in the process.
    """
    with django_db_blocker.unblock():
        yield
