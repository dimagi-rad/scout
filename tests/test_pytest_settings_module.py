from django.conf import settings


def test_suite_runs_under_test_settings_even_with_env_override(pytestconfig):
    # A sourced .env exports config.settings.development; under it the caplog
    # tests failed and the suite ran against development's database settings.
    # CI exports the test module anyway, so pin the option itself as well: dropping
    # --ds from addopts must fail here, not only on a developer's machine.
    assert pytestconfig.getoption("ds") == "config.settings.test"
    assert settings.SETTINGS_MODULE == "config.settings.test"
