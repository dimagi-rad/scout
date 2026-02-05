"""
Authentication helpers for the Scout Chainlit application.

Provides authentication callbacks for different deployment scenarios:
- password_auth_callback: Simple username/password for development
- oauth_callback: OAuth integration via Django allauth for production
- header_auth_callback: Trusted header auth for reverse proxy setups
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import chainlit as cl

if TYPE_CHECKING:
    from apps.users.models import User

logger = logging.getLogger(__name__)


async def password_auth_callback(username: str, password: str) -> cl.User | None:
    """
    Authenticate users with username/password for development.

    This callback validates credentials against the Django User model.
    In development, it also accepts a configured dev user for quick access.

    Args:
        username: Email address of the user.
        password: Plain text password to verify.

    Returns:
        A Chainlit User object if authentication succeeds, None otherwise.
    """
    # Import Django models (requires Django setup)
    from django.contrib.auth import authenticate

    from apps.users.models import User

    # Check for development backdoor (only in DEBUG mode)
    dev_username = os.environ.get("CHAINLIT_DEV_USERNAME")
    dev_password = os.environ.get("CHAINLIT_DEV_PASSWORD")

    if dev_username and dev_password:
        if username == dev_username and password == dev_password:
            # Try to find or create the dev user
            try:
                user = User.objects.get(email=dev_username)
            except User.DoesNotExist:
                logger.warning(
                    "Dev user %s not found in database. Create the user first.",
                    dev_username,
                )
                return None

            logger.info("Dev user authenticated: %s", username)
            return cl.User(
                identifier=str(user.id),
                metadata={
                    "email": user.email,
                    "name": user.get_full_name(),
                    "provider": "dev",
                },
            )

    # Standard Django authentication
    user = authenticate(username=username, password=password)

    if user is None:
        logger.warning("Authentication failed for user: %s", username)
        return None

    if not user.is_active:
        logger.warning("Inactive user attempted login: %s", username)
        return None

    logger.info("User authenticated: %s", username)
    return cl.User(
        identifier=str(user.id),
        metadata={
            "email": user.email,
            "name": user.get_full_name(),
            "provider": "password",
        },
    )


async def oauth_callback(
    provider_id: str,
    token: str,
    raw_user_data: dict,
    default_user: cl.User,
) -> cl.User | None:
    """
    Handle OAuth authentication via Django allauth.

    This callback is invoked after successful OAuth flow. It looks up the
    Django user associated with the OAuth account via allauth's SocialAccount.

    Args:
        provider_id: OAuth provider identifier (e.g., "google", "github").
        token: OAuth access token (for API calls if needed).
        raw_user_data: Raw user data from the OAuth provider.
        default_user: Default Chainlit user constructed from OAuth data.

    Returns:
        A Chainlit User object linked to the Django user, or None if not found.
    """
    from allauth.socialaccount.models import SocialAccount

    from apps.users.models import User

    # Extract the provider's user ID
    provider_user_id = raw_user_data.get("id") or raw_user_data.get("sub")

    if not provider_user_id:
        logger.error("OAuth callback missing user ID in raw_user_data: %s", raw_user_data)
        return None

    try:
        # Look up the Django user via allauth's SocialAccount
        social_account = SocialAccount.objects.select_related("user").get(
            provider=provider_id,
            uid=str(provider_user_id),
        )
        user = social_account.user

        logger.info("OAuth user found: %s via %s", user.email, provider_id)
        return cl.User(
            identifier=str(user.id),
            metadata={
                "email": user.email,
                "name": user.get_full_name(),
                "provider": provider_id,
                "provider_user_id": str(provider_user_id),
            },
        )

    except SocialAccount.DoesNotExist:
        logger.warning(
            "No Django user found for OAuth account: provider=%s, uid=%s",
            provider_id,
            provider_user_id,
        )
        # In production, you might want to auto-create users here
        # For now, require pre-existing accounts
        return None

    except Exception as e:
        logger.exception("Error during OAuth callback: %s", e)
        return None


async def header_auth_callback(headers: dict) -> cl.User | None:
    """
    Authenticate users via trusted proxy headers.

    This callback is used when Chainlit sits behind a reverse proxy
    (e.g., nginx, Traefik) that handles authentication and passes
    user identity in headers.

    Expected headers (configurable via environment):
    - X-Auth-User-Id: User's UUID or primary key
    - X-Auth-User-Email: User's email address
    - X-Auth-User-Name: User's display name (optional)

    Args:
        headers: HTTP headers from the request.

    Returns:
        A Chainlit User object if valid headers are present, None otherwise.
    """
    from apps.users.models import User

    # Header names (configurable via environment)
    user_id_header = os.environ.get("AUTH_USER_ID_HEADER", "X-Auth-User-Id")
    user_email_header = os.environ.get("AUTH_USER_EMAIL_HEADER", "X-Auth-User-Email")
    user_name_header = os.environ.get("AUTH_USER_NAME_HEADER", "X-Auth-User-Name")

    # Normalize header keys (HTTP headers are case-insensitive)
    normalized_headers = {k.lower(): v for k, v in headers.items()}

    user_id = normalized_headers.get(user_id_header.lower())
    user_email = normalized_headers.get(user_email_header.lower())
    user_name = normalized_headers.get(user_name_header.lower(), "")

    if not user_id and not user_email:
        logger.debug("Header auth: no user identification headers found")
        return None

    try:
        # Look up user by ID or email
        if user_id:
            user = User.objects.get(pk=user_id)
        else:
            user = User.objects.get(email=user_email)

        if not user.is_active:
            logger.warning("Header auth: inactive user %s", user.email)
            return None

        logger.info("Header auth: user authenticated: %s", user.email)
        return cl.User(
            identifier=str(user.id),
            metadata={
                "email": user.email,
                "name": user_name or user.get_full_name(),
                "provider": "header",
            },
        )

    except User.DoesNotExist:
        logger.warning(
            "Header auth: user not found (id=%s, email=%s)",
            user_id,
            user_email,
        )
        return None

    except Exception as e:
        logger.exception("Error during header auth: %s", e)
        return None


def get_django_user(cl_user: cl.User) -> "User | None":
    """
    Retrieve the Django User model instance from a Chainlit User.

    Helper function used throughout the application to get the full
    Django user object for database operations.

    Args:
        cl_user: The Chainlit User object from the session.

    Returns:
        The Django User model instance, or None if not found.
    """
    from apps.users.models import User

    try:
        return User.objects.get(pk=cl_user.identifier)
    except User.DoesNotExist:
        logger.error("Django user not found for Chainlit user: %s", cl_user.identifier)
        return None
    except Exception as e:
        logger.exception("Error retrieving Django user: %s", e)
        return None
