"""Tests for the pre_social_login handler that backfills/merges emails."""

from types import SimpleNamespace

import pytest
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model

from apps.users.signals import reconcile_existing_user_on_login

User = get_user_model()


def _sociallogin(user, extra_data):
    """Build a SocialLogin-shaped stub. Handler only touches .user and .account."""
    return SimpleNamespace(
        user=user,
        account=SimpleNamespace(extra_data=extra_data, user=user),
    )


@pytest.mark.django_db
def test_brand_new_user_is_noop():
    new_user = User(email=None)  # unsaved -> pk is None
    sl = _sociallogin(new_user, {"email": "x@y.com"})

    reconcile_existing_user_on_login(sender=None, request=None, sociallogin=sl)

    # No DB writes; new_user remains unsaved
    assert new_user.pk is None


@pytest.mark.django_db
def test_existing_user_with_email_is_noop():
    existing = User.objects.create(email="brian@y.com", username="b")
    sl = _sociallogin(existing, {"email": "other@y.com"})

    reconcile_existing_user_on_login(sender=None, request=None, sociallogin=sl)

    existing.refresh_from_db()
    assert existing.email == "brian@y.com"  # untouched


@pytest.mark.django_db
def test_no_email_in_extra_data_is_noop():
    existing = User.objects.create(email=None, username="b")
    sl = _sociallogin(existing, {})  # no email key

    reconcile_existing_user_on_login(sender=None, request=None, sociallogin=sl)

    existing.refresh_from_db()
    assert existing.email is None


@pytest.mark.django_db
def test_no_collision_backfills_user_email():
    existing = User.objects.create(email=None, username="connect-user")
    sl = _sociallogin(existing, {"email": "brian@y.com"})

    reconcile_existing_user_on_login(sender=None, request=None, sociallogin=sl)

    existing.refresh_from_db()
    assert existing.email == "brian@y.com"


@pytest.mark.django_db
def test_collision_merges_user_and_redirects_session():
    canonical = User.objects.create(email="brian@y.com", username="canon")
    duplicate = User.objects.create(email=None, username="connect-user")
    dup_account = SocialAccount.objects.create(
        user=duplicate, provider="commcare_connect", uid="999",
        extra_data={"email": "brian@y.com"},
    )
    sl = SimpleNamespace(user=duplicate, account=dup_account)

    reconcile_existing_user_on_login(sender=None, request=None, sociallogin=sl)

    # duplicate was merged away
    assert not User.objects.filter(pk=duplicate.pk).exists()
    # Connect SocialAccount now points at canonical
    dup_account.refresh_from_db()
    assert dup_account.user == canonical
    # Session redirected
    assert sl.user == canonical
    assert sl.account.user == canonical


@pytest.mark.django_db
def test_collision_match_is_case_insensitive():
    canonical = User.objects.create(email="Brian@Y.com", username="canon")
    duplicate = User.objects.create(email=None, username="dup")
    dup_account = SocialAccount.objects.create(
        user=duplicate, provider="commcare_connect", uid="x",
        extra_data={"email": "brian@y.com"},
    )
    sl = SimpleNamespace(user=duplicate, account=dup_account)

    reconcile_existing_user_on_login(sender=None, request=None, sociallogin=sl)

    assert not User.objects.filter(pk=duplicate.pk).exists()
    assert sl.user == canonical
