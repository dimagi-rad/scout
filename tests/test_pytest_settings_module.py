from django.conf import settings


def test_suite_runs_under_test_settings_even_with_env_override():
    # A sourced .env exports config.settings.development; under it the caplog
    # tests failed and the suite ran against development's database settings.
    assert settings.SETTINGS_MODULE == "config.settings.test"
