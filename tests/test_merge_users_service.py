"""Unit tests for apps.users.services.merge.merge_users and helpers."""

import pytest
from django.contrib.auth import get_user_model

from apps.users.services.merge import select_canonical

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
