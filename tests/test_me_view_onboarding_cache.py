"""me_view onboarding caching + flap fix (arch #254, finding 07#4).

``me_view`` recomputed onboarding from the DB on every ``/me`` poll and, when
incomplete, eagerly re-hit all three provider APIs with no cache guard. Worse,
``_atry_resolve_provider`` returned ``True`` whenever a token existed and the
resolver didn't raise — even if it resolved zero memberships
(``resolve_commcare_domains`` returns ``[]`` without raising) — so the flag
flapped ``True`` transiently while the persisted state stayed incomplete, and
the SPA re-resolved on every poll.

Fix: only report complete when memberships are actually persisted, and cache the
computed result (short TTL) so a poll loop doesn't re-resolve providers.
"""

import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.core.cache import cache

User = get_user_model()


@pytest.fixture
def site(db):
    site, _ = Site.objects.get_or_create(id=1, defaults={"domain": "testserver", "name": "Test"})
    return site


@pytest.fixture
def commcare_app(site):
    app = SocialApp.objects.create(provider="commcare", name="CommCare", client_id="cc", secret="s")
    app.sites.add(site)
    return app


@pytest.fixture
def token_only_user(db, commcare_app):
    """A user with a CommCare token but ZERO tenant memberships."""
    user = User.objects.create_user(email="tokenonly@example.com", password="pass")
    acct = SocialAccount.objects.create(user=user, provider="commcare", uid="cc-uid")
    SocialToken.objects.create(app=commcare_app, account=acct, token="cc-token")
    return user


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_onboarding_not_complete_when_zero_memberships_resolved(client, token_only_user):
    """A token-bearing user whose resolver yields zero memberships must report
    onboarding_complete=False (no flap to True) — 07#4."""
    client.force_login(token_only_user)

    async def resolve_nothing(user, token):
        return []  # token exists, resolves zero opportunities, does not raise

    with (
        pytest.MonkeyPatch().context() as mp,
    ):
        mp.setattr("apps.users.auth_views.resolve_commcare_domains", resolve_nothing)
        mp.setattr("apps.users.auth_views.resolve_connect_opportunities", resolve_nothing)
        mp.setattr("apps.users.auth_views.resolve_ocs_chatbots", resolve_nothing)
        resp = client.get("/api/auth/me/")

    assert resp.status_code == 200
    assert resp.json()["onboarding_complete"] is False


@pytest.mark.django_db
def test_me_view_served_from_cache_on_second_call(client, token_only_user):
    """The second /me poll within the cache TTL must not re-hit providers."""
    client.force_login(token_only_user)

    calls = {"n": 0}

    async def resolve_counting(user, token):
        calls["n"] += 1
        return []

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("apps.users.auth_views.resolve_commcare_domains", resolve_counting)
        mp.setattr("apps.users.auth_views.resolve_connect_opportunities", resolve_counting)
        mp.setattr("apps.users.auth_views.resolve_ocs_chatbots", resolve_counting)

        client.get("/api/auth/me/")
        first = calls["n"]
        assert first > 0, "first call should attempt provider resolution"

        client.get("/api/auth/me/")
        second = calls["n"]

    # The second poll is served from cache; no further provider calls.
    assert second == first
