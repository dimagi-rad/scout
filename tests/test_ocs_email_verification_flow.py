"""OCS emails link accounts only when OCS asserts ``email_verified`` (R20).

These drive allauth's real login pipeline (``sociallogin_from_response`` ->
``cleanup_email_addresses`` -> ``complete_social_login``), because a blanket
``VERIFIED_EMAIL`` setting used to re-mark every OCS email verified *after* the
provider had correctly reported it unverified.
"""

from types import SimpleNamespace

import pytest
from allauth.account.models import EmailAddress
from allauth.core.context import request_context
from allauth.socialaccount.helpers import complete_social_login
from allauth.socialaccount.models import SocialAccount, SocialApp
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware

from apps.users.providers.ocs.provider import OCSProvider
from apps.users.services.email_proof import proven_emails, verified_social_email
from apps.users.signals import reconcile_existing_user_on_login

User = get_user_model()

VICTIM_EMAIL = "victim@dimagi.com"


@pytest.fixture
def victim(db):
    """An existing Scout account that proved its email through CommCare HQ."""
    user = User.objects.create(email=VICTIM_EMAIL, username="victim")
    EmailAddress.objects.create(user=user, email=VICTIM_EMAIL, verified=True, primary=True)
    SocialAccount.objects.create(
        user=user, provider="commcare", uid="hq-1", extra_data={"email": VICTIM_EMAIL}
    )
    return user


def _userinfo(**claims):
    return {"sub": "ocs-attacker", "team": "acme", "email": VICTIM_EMAIL, **claims}


def _login(rf, ocs_app, userinfo):
    request = rf.get("/accounts/ocs/login/callback/")
    SessionMiddleware(lambda request: None).process_request(request)
    MessageMiddleware(lambda request: None).process_request(request)
    request.user = AnonymousUser()
    with request_context(request):
        sociallogin = OCSProvider(request, app=ocs_app).sociallogin_from_response(request, userinfo)
        response = complete_social_login(request, sociallogin)
    return request, sociallogin, response


@pytest.mark.django_db
@pytest.mark.parametrize(
    "claims", [{"email_verified": False}, {}, {"email_verified": "true"}], ids=str
)
def test_unverified_ocs_email_is_dropped_by_the_full_cleanup(rf, ocs_app, claims):
    request = rf.get("/")
    with request_context(request):
        sociallogin = OCSProvider(request, app=ocs_app).sociallogin_from_response(
            request, _userinfo(**claims)
        )

    assert sociallogin.email_addresses == []
    assert not sociallogin.user.email


@pytest.mark.django_db
@pytest.mark.parametrize("claims", [{"email_verified": False}, {}], ids=["false", "absent"])
def test_unverified_ocs_login_does_not_link_to_the_email_owner(rf, ocs_app, victim, claims):
    request, _sociallogin, response = _login(rf, ocs_app, _userinfo(**claims))

    assert response.status_code == 302
    ocs_account = SocialAccount.objects.get(provider="ocs")
    assert ocs_account.user_id != victim.pk
    assert request.session["_auth_user_id"] == str(ocs_account.user_id)
    assert not ocs_account.user.email
    assert VICTIM_EMAIL not in proven_emails(ocs_account.user)


@pytest.mark.django_db
def test_verified_ocs_login_links_to_the_email_owner(rf, ocs_app, victim):
    request, _sociallogin, response = _login(rf, ocs_app, _userinfo(email_verified=True))

    assert response.status_code == 302
    assert SocialAccount.objects.get(provider="ocs").user_id == victim.pk
    assert request.session["_auth_user_id"] == str(victim.pk)


@pytest.mark.django_db
def test_existing_ocs_user_is_not_merged_on_an_unverified_email(rf, ocs_app, victim):
    emailless = User.objects.create(email=None, username="ocs-user")
    SocialAccount.objects.create(
        user=emailless, provider="ocs", uid="ocs-attacker#acme", extra_data={"sub": "ocs-attacker"}
    )

    _login(rf, ocs_app, _userinfo(email_verified=False))

    assert User.objects.filter(pk=emailless.pk).exists()
    assert SocialAccount.objects.get(provider="ocs").user_id == emailless.pk
    emailless.refresh_from_db()
    assert emailless.email is None


@pytest.mark.django_db
def test_existing_ocs_user_is_merged_on_a_verified_email(rf, ocs_app, victim):
    emailless = User.objects.create(email=None, username="ocs-user")
    SocialAccount.objects.create(
        user=emailless, provider="ocs", uid="ocs-attacker#acme", extra_data={"sub": "ocs-attacker"}
    )

    request, _sociallogin, _response = _login(rf, ocs_app, _userinfo(email_verified=True))

    assert not User.objects.filter(pk=emailless.pk).exists()
    assert SocialAccount.objects.get(provider="ocs").user_id == victim.pk
    assert request.session["_auth_user_id"] == str(victim.pk)


def _stored_login(user, provider, extra_data):
    account = SocialAccount.objects.create(
        user=user, provider=provider, uid=f"{provider}-{user.pk}", extra_data=extra_data
    )
    return SimpleNamespace(user=user, account=account)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "provider, extra_data",
    [
        ("ocs", {"email": "new@dimagi.com"}),
        ("ocs", {"email": "new@dimagi.com", "email_verified": False}),
        ("github", {"email": "new@dimagi.com"}),
    ],
    ids=["ocs-absent", "ocs-false", "untrusted"],
)
def test_unverified_email_is_not_backfilled(provider, extra_data):
    user = User.objects.create(email=None, username="no-email")

    reconcile_existing_user_on_login(
        sender=None, request=None, sociallogin=_stored_login(user, provider, extra_data)
    )

    user.refresh_from_db()
    assert user.email is None


@pytest.mark.django_db
def test_verified_ocs_email_is_backfilled():
    user = User.objects.create(email=None, username="no-email")
    extra_data = {"email": "new@dimagi.com", "email_verified": True}

    reconcile_existing_user_on_login(
        sender=None, request=None, sociallogin=_stored_login(user, "ocs", extra_data)
    )

    user.refresh_from_db()
    assert user.email == "new@dimagi.com"
    assert "new@dimagi.com" in proven_emails(user)


@pytest.mark.django_db
@pytest.mark.parametrize("claim, merged", [(False, False), (None, False), (True, True)])
def test_canonical_owning_email_only_via_ocs_needs_the_claim(claim, merged):
    canonical = User.objects.create(email=VICTIM_EMAIL, username="canon")
    stored = {"email": VICTIM_EMAIL}
    if claim is not None:
        stored["email_verified"] = claim
    SocialAccount.objects.create(user=canonical, provider="ocs", uid="o#t", extra_data=stored)
    duplicate = User.objects.create(email=None, username="connect-user")
    login = _stored_login(duplicate, "commcare_connect", {"email": VICTIM_EMAIL})

    reconcile_existing_user_on_login(sender=None, request=None, sociallogin=login)

    assert User.objects.filter(pk=duplicate.pk).exists() is not merged
    assert (login.user == canonical) is merged


@pytest.mark.django_db
def test_trusted_providers_vouch_without_a_claim():
    for provider in ("commcare", "commcare_eu", "commcare_connect"):
        assert verified_social_email(provider, {"email": "a@b.co"}) == "a@b.co"
    assert verified_social_email("ocs", {"email": "a@b.co"}) is None
    assert verified_social_email("ocs", {"email": "a@b.co", "email_verified": True}) == "a@b.co"
    assert verified_social_email("commcare", {}) is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "alias, cls, extra_data, vouched",
    [
        ("hq_production", "commcare", {"email": " a@b.co "}, True),
        ("commcare_prod", "commcare", {"email": "a@b.co"}, True),
        ("connect_production", "commcare_connect", {"email": "a@b.co"}, True),
        ("ocs_staging", "ocs", {"email": "a@b.co"}, False),
        ("ocs_staging", "ocs", {"email": "a@b.co", "email_verified": True}, True),
        ("chat_prod", "ocs", {"email": "a@b.co"}, False),
        ("commcare_ocs", "ocs", {"email": "a@b.co"}, False),
        ("commcare_orphan", None, {"email": "a@b.co"}, False),
        ("commcare_eu", None, {"email": "a@b.co"}, True),
    ],
)
def test_aliased_provider_ids_resolve_to_their_class(alias, cls, extra_data, vouched):
    """SocialAccount.provider stores a SocialApp's provider_id alias, not the class id."""
    if cls:
        SocialApp.objects.create(provider=cls, provider_id=alias, name=alias, client_id=alias)
    user = User.objects.create(email=None, username="aliased")
    SocialAccount.objects.create(user=user, provider=alias, uid="u", extra_data=extra_data)

    assert (verified_social_email(alias, extra_data) == "a@b.co") is vouched
    assert ("a@b.co" in proven_emails(user)) is vouched


@pytest.mark.django_db
@pytest.mark.parametrize("email", [["a@b.co"], 7, "   "])
def test_malformed_email_is_not_vouched(email):
    assert verified_social_email("commcare", {"email": email}) is None


@pytest.mark.django_db
def test_alias_claimed_by_apps_of_different_classes_is_untrusted():
    for cls in ("commcare", "ocs"):
        SocialApp.objects.create(provider=cls, provider_id="shared", name=cls, client_id=cls)

    assert verified_social_email("shared", {"email": "a@b.co"}) is None


@pytest.mark.django_db
def test_ocs_is_never_blanket_trusted(settings):
    settings.SOCIALACCOUNT_PROVIDERS = {
        **settings.SOCIALACCOUNT_PROVIDERS,
        "ocs": {"VERIFIED_EMAIL": True},
    }

    assert verified_social_email("ocs", {"email": "a@b.co"}) is None


@pytest.mark.django_db
@pytest.mark.parametrize("extra_data", [["a@b.co"], "a@b.co", None])
def test_non_dict_extra_data_is_not_vouched(extra_data):
    assert verified_social_email("commcare", extra_data) is None
