"""Users whose email counts as proven, for tests that exercise invite matching."""

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model


def create_verified_user(email: str, password: str = "pass"):
    user = get_user_model().objects.create_user(email=email, password=password)
    EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    return user
