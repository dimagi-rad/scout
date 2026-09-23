from unittest.mock import Mock

import pytest
from django.core.checks import Tags, run_checks

from apps.semantic.checks import check_cube_configuration
from tasks import deps


@pytest.fixture(autouse=True)
def configured_cube(settings):
    settings.DEBUG = True
    settings.CUBE_API_URL = "http://localhost:4000"
    settings.CUBE_VALIDATOR_URL = "http://localhost:4010"
    settings.CUBEJS_API_SECRET = "test-only-secret"


@pytest.mark.parametrize("name", ["CUBE_API_URL", "CUBE_VALIDATOR_URL", "CUBEJS_API_SECRET"])
def test_missing_cube_setting_has_actionable_warning(settings, name):
    setattr(settings, name, "")
    warnings = check_cube_configuration(None)
    assert len(warnings) == 1
    assert warnings[0].id == "semantic.W001"
    assert name in warnings[0].msg
    assert "platform-db cube" in warnings[0].hint
    assert "test-only-secret" not in str(warnings[0])


def test_configured_cube_does_not_warn():
    assert check_cube_configuration(None) == []


def test_check_is_registered(settings):
    settings.CUBE_API_URL = ""
    assert any(w.id == "semantic.W001" for w in run_checks(tags=[Tags.compatibility]))


def test_local_setup_warning_is_not_applied_to_production(settings):
    settings.DEBUG = False
    settings.CUBE_API_URL = ""
    assert check_cube_configuration(None) == []


def test_dependency_command_starts_cube_without_a_duplicate_mcp():
    context = Mock()
    deps.body(context)
    context.run.assert_called_once_with(
        "docker compose up -d --build --wait platform-db cube", pty=True
    )
