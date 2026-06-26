"""Tests for the setup_oauth_apps management command (arch #260, 11#7).

Google/GitHub composed a double-``_OAUTH_`` env-var name and could never
bootstrap. These tests pin the correct env-var spelling for every provider.
"""

import pytest
from allauth.socialaccount.models import SocialApp
from django.core.management import call_command


@pytest.mark.django_db
class TestSetupOAuthApps:
    def test_google_bootstraps_with_documented_env_vars(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "g-id")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "g-secret")

        call_command("setup_oauth_apps")

        app = SocialApp.objects.get(provider="google")
        assert app.client_id == "g-id"
        assert app.secret == "g-secret"

    def test_github_bootstraps_with_documented_env_vars(self, monkeypatch):
        monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "gh-id")
        monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "gh-secret")

        call_command("setup_oauth_apps")

        app = SocialApp.objects.get(provider="github")
        assert app.client_id == "gh-id"
        assert app.secret == "gh-secret"

    def test_double_oauth_spelling_is_not_read(self, monkeypatch):
        # The old buggy name must NOT bootstrap the app.
        monkeypatch.setenv("GOOGLE_OAUTH_OAUTH_CLIENT_ID", "wrong")
        monkeypatch.setenv("GOOGLE_OAUTH_OAUTH_CLIENT_SECRET", "wrong")

        call_command("setup_oauth_apps")

        assert not SocialApp.objects.filter(provider="google").exists()

    def test_commcare_connect_ocs_use_deploy_var_names(self, monkeypatch):
        # Mirrors config/deploy.yml: {PREFIX}_OAUTH_CLIENT_ID.
        for prefix in ("COMMCARE", "CONNECT", "OCS"):
            monkeypatch.setenv(f"{prefix}_OAUTH_CLIENT_ID", f"{prefix}-id")
            monkeypatch.setenv(f"{prefix}_OAUTH_CLIENT_SECRET", f"{prefix}-secret")

        call_command("setup_oauth_apps")

        assert SocialApp.objects.get(provider="commcare").client_id == "COMMCARE-id"
        assert SocialApp.objects.get(provider="commcare_connect").client_id == "CONNECT-id"
        assert SocialApp.objects.get(provider="ocs").client_id == "OCS-id"

    def test_skip_message_names_the_real_env_var(self, monkeypatch, capsys):
        # No env vars set for google -> skip line must reference the real name.
        for var in (
            "GOOGLE_OAUTH_CLIENT_ID",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "GOOGLE_OAUTH_OAUTH_CLIENT_ID",
        ):
            monkeypatch.delenv(var, raising=False)

        call_command("setup_oauth_apps")

        out = capsys.readouterr().out
        assert "GOOGLE_OAUTH_CLIENT_ID" in out
        assert "GOOGLE_OAUTH_OAUTH_CLIENT_ID" not in out
