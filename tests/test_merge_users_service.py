"""Unit tests for apps.users.services.merge.merge_users and helpers."""

import pytest
from django.contrib.auth import get_user_model

from apps.users.services.merge import merge_users, select_canonical

User = get_user_model()


@pytest.mark.django_db
def test_select_canonical_prefers_usable_password():
    no_pw = User.objects.create(email="a@y.com", username="a")
    no_pw.set_unusable_password()
    no_pw.save()
    with_pw = User.objects.create(email="b@y.com", username="b")
    with_pw.set_password("real-password")
    with_pw.save()

    assert select_canonical([no_pw, with_pw]) == with_pw


@pytest.mark.django_db
def test_select_canonical_prefers_oldest_when_passwords_equal():
    older = User.objects.create(email="older@y.com", username="older")
    older.set_password("pw")
    older.save()
    newer = User.objects.create(email="newer@y.com", username="newer")
    newer.set_password("pw")
    newer.save()

    assert select_canonical([newer, older]) == older


@pytest.mark.django_db
def test_field_level_merge_copies_password_from_duplicate():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    canonical.set_unusable_password()
    canonical.save()
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    duplicate.set_password("real-password")
    duplicate.save()
    dup_hash = duplicate.password

    merge_users(canonical=canonical, duplicate=duplicate)

    canonical.refresh_from_db()
    assert canonical.password == dup_hash
    assert canonical.has_usable_password()


@pytest.mark.django_db
def test_field_level_merge_ors_staff_and_superuser_flags():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(
        email="dup@y.com", username="dup", is_staff=True, is_superuser=True,
    )

    merge_users(canonical=canonical, duplicate=duplicate)

    canonical.refresh_from_db()
    assert canonical.is_staff is True
    assert canonical.is_superuser is True


@pytest.mark.django_db
def test_field_level_merge_fills_empty_name_fields_from_duplicate():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(
        email="dup@y.com", username="dup",
        first_name="Brian", last_name="DeRenzi", avatar_url="https://x/y.png",
    )

    merge_users(canonical=canonical, duplicate=duplicate)

    canonical.refresh_from_db()
    assert canonical.first_name == "Brian"
    assert canonical.last_name == "DeRenzi"
    assert canonical.avatar_url == "https://x/y.png"


@pytest.mark.django_db
def test_field_level_merge_keeps_canonical_name_when_already_set():
    canonical = User.objects.create(
        email="canon@y.com", username="canon", first_name="Already", last_name="Set",
    )
    duplicate = User.objects.create(
        email="dup@y.com", username="dup", first_name="Newer", last_name="Name",
    )

    merge_users(canonical=canonical, duplicate=duplicate)

    canonical.refresh_from_db()
    assert canonical.first_name == "Already"
    assert canonical.last_name == "Set"
