"""Guards that keep CI honest about running the real-DB suites (arch issue #233)."""

import os

import pytest


@pytest.mark.skipif(not os.environ.get("CI"), reason="only enforced in CI")
def test_managed_database_url_set_in_ci():
    """MANAGED_DATABASE_URL must be set in CI.

    Without it, the view schema, OCS materializer, materializer writer, and MCP query
    integration tests skip — a green badge
    over untested real-DB code (arch findings 12#2, 10#3). GitHub Actions sets CI=true,
    so this assertion runs there and is skipped locally.
    """
    assert os.environ.get("MANAGED_DATABASE_URL"), (
        "MANAGED_DATABASE_URL is unset in CI; the real-DB regression suites would "
        "silently skip. Set it in .github/workflows/test.yml."
    )
