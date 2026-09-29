"""Deactivating a user ends their sessions (#385)."""

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.test import Client


def test_session_cookie_age_is_two_weeks():
    assert settings.SESSION_COOKIE_AGE == 14 * 24 * 3600


@pytest.fixture
def other_user(db):
    return get_user_model().objects.create_user(email="other@example.com", password="pw12345678")


@pytest.mark.django_db
def test_deactivation_ends_existing_session(user):
    client = Client()
    client.force_login(user)
    assert client.get("/api/auth/me/").status_code == 200

    user.is_active = False
    user.save()

    assert client.get("/api/auth/me/").status_code in (401, 403)
    assert Session.objects.count() == 0


@pytest.mark.django_db
def test_deactivation_leaves_other_users_sessions(user, other_user):
    other_client = Client()
    other_client.force_login(other_user)
    Client().force_login(user)

    user.is_active = False
    user.save()

    assert Session.objects.count() == 1
    assert other_client.get("/api/auth/me/").status_code == 200


@pytest.mark.django_db
def test_saving_active_user_keeps_session(user):
    client = Client()
    client.force_login(user)
    user.first_name = "Changed"
    user.save()
    assert client.get("/api/auth/me/").status_code == 200
