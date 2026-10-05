"""Guard that DEPLOY_ENVIRONMENT labels prod-posture settings modules correctly.

Issue #248, finding 08#5: DEPLOY_ENVIRONMENT was 'production' only when the
settings module ended in '.production'. The connectlabs module
('config.settings.connectlabs') inherits the full production security posture
but was mislabeled 'development' for Sentry / Task Badger, so labs errors and
task telemetry were tagged as a dev environment.

These tests exercise the pure resolver and do NOT need a database.
"""

import pytest

from config.settings.base import resolve_deploy_environment


@pytest.mark.parametrize(
    ("settings_module", "expected"),
    [
        ("config.settings.production", "production"),
        ("config.settings.connectlabs", "production"),
        ("config.settings.development", "development"),
        ("config.settings.test", "development"),
        ("", "development"),
    ],
)
def test_resolve_deploy_environment(settings_module, expected):
    assert resolve_deploy_environment(settings_module) == expected
