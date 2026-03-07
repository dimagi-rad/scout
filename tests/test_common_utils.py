"""Tests for common utility functions."""

import pytest

from apps.common.utils import creator_display_name


@pytest.mark.django_db
def test_creator_display_name_with_user(user):
    assert creator_display_name(user) == user.get_full_name()


def test_creator_display_name_with_none():
    assert creator_display_name(None) == "Deleted user"
