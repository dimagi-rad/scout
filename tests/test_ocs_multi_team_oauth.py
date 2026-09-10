"""Tests for multi-token OAuth: N team-scoped connections per (user, provider) (#156)."""

from __future__ import annotations

import importlib

import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp
from django.apps import apps as global_apps

from apps.users.providers.ocs.provider import OCSProvider, team_slug_from_uid


def _provider() -> OCSProvider:
    app = SocialApp(provider=OCSProvider.id, name="OCS", client_id="x", secret="x")
    return OCSProvider(request=None, app=app)


class TestTeamQualifiedUid:
    def test_uid_carries_the_team_the_token_is_scoped_to(self):
        uid = _provider().extract_uid({"sub": "42", "team": "acme"})
        assert uid == "42#acme"
        assert team_slug_from_uid(uid) == "acme"

    def test_two_teams_of_one_ocs_user_get_distinct_uids(self):
        provider = _provider()
        assert provider.extract_uid({"sub": "42", "team": "acme"}) != provider.extract_uid(
            {"sub": "42", "team": "globex"}
        )

    def test_no_team_claim_keeps_the_bare_subject(self):
        """An OCS deploy that doesn't emit the claim must behave exactly as before."""
        assert _provider().extract_uid({"sub": "42"}) == "42"
        assert _provider().extract_uid({"sub": "42", "team": "  "}) == "42"
        assert team_slug_from_uid("42") == ""

    def test_missing_subject_still_raises(self):
        with pytest.raises(ValueError, match="Cannot determine UID"):
            _provider().extract_uid({"team": "acme"})


@pytest.mark.django_db
class TestUidQualificationMigration:
    """0010 rewrites bare-``sub`` OCS uids so returning users still match."""

    @staticmethod
    def _module():
        return importlib.import_module(
            "apps.users.migrations.0010_qualify_ocs_social_account_uid_by_team"
        )

    def test_forward_qualifies_an_existing_account_and_backward_restores_it(self, user):
        acct = SocialAccount.objects.create(
            user=user, provider="ocs", uid="42", extra_data={"team": "acme"}
        )
        mod = self._module()

        mod.forward(global_apps, None)
        acct.refresh_from_db()
        assert acct.uid == "42#acme"

        mod.backward(global_apps, None)
        acct.refresh_from_db()
        assert acct.uid == "42"

    def test_forward_is_idempotent_and_leaves_claimless_rows_alone(self, user, other_user):
        qualified = SocialAccount.objects.create(
            user=user, provider="ocs", uid="42#acme", extra_data={"team": "acme"}
        )
        claimless = SocialAccount.objects.create(
            user=other_user, provider="ocs", uid="99", extra_data={}
        )
        mod = self._module()

        mod.forward(global_apps, None)
        mod.forward(global_apps, None)

        qualified.refresh_from_db()
        claimless.refresh_from_db()
        assert qualified.uid == "42#acme"
        assert claimless.uid == "99"
