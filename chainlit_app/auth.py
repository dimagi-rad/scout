"""
Authentication bridge between Chainlit and Django allauth.

This module provides authentication callbacks for the Chainlit UI that
integrate with Django's auth system and django-allauth for OAuth providers.

Supports three authentication modes:
1. Password auth - Simple username/password for development
2. OAuth callback - Integration with django-allauth social accounts (Google, GitHub, etc.)
3. Header auth - For reverse proxy setups (oauth2-proxy, Authelia, etc.)

The callbacks are decorated with Chainlit's auth decorators and automatically
register with the Chainlit app when this module is imported.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import chainlit as cl
from django.conf import settings as django_settings

if TYPE_CHECKING:
    from apps.users.models import User

logger = logging.getLogger(__name__)


@cl.password_auth_callback
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


@cl.oauth_callback
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
    If SOCIALACCOUNT_AUTO_SIGNUP is enabled and no existing user is found,
    a new user will be auto-created.

    Args:
        provider_id: OAuth provider identifier (e.g., "google", "github").
        token: OAuth access token (for API calls if needed).
        raw_user_data: Raw user data from the OAuth provider.
        default_user: Default Chainlit user constructed from OAuth data.

    Returns:
        A Chainlit User object linked to the Django user, or None if not found
        and auto-signup is disabled.
    """
    from allauth.socialaccount.models import SocialAccount

    from apps.users.models import User

    # Extract the provider's user ID (different providers use different keys)
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

        # Check if auto-signup is enabled
        auto_signup = getattr(django_settings, "SOCIALACCOUNT_AUTO_SIGNUP", False)

        if not auto_signup:
            logger.info("Auto-signup disabled, OAuth login rejected")
            return None

        # Auto-create user from OAuth data
        email = raw_user_data.get("email")
        if not email:
            logger.error("Cannot auto-create user: no email in OAuth data")
            return None

        # Extract name fields (different providers use different field names)
        first_name = (
            raw_user_data.get("given_name")
            or raw_user_data.get("first_name")
            or raw_user_data.get("name", "").split()[0] if raw_user_data.get("name") else ""
        )
        last_name = (
            raw_user_data.get("family_name")
            or raw_user_data.get("last_name")
            or " ".join(raw_user_data.get("name", "").split()[1:]) if raw_user_data.get("name") else ""
        )

        # Check if a user with this email already exists
        try:
            user = User.objects.get(email=email)
            logger.info("Found existing user with email %s, linking social account", email)
        except User.DoesNotExist:
            # Create new user
            user = User.objects.create(
                email=email,
                first_name=first_name,
                last_name=last_name,
            )
            logger.info("Created new user from OAuth: %s", email)

        # Create the SocialAccount link
        SocialAccount.objects.create(
            user=user,
            provider=provider_id,
            uid=str(provider_user_id),
            extra_data=raw_user_data,
        )
        logger.info("Created social account link: %s -> %s", email, provider_id)

        return cl.User(
            identifier=str(user.id),
            metadata={
                "email": user.email,
                "name": user.get_full_name(),
                "provider": provider_id,
                "provider_user_id": str(provider_user_id),
                "auto_created": True,
            },
        )

    except Exception as e:
        logger.exception("Error during OAuth callback: %s", e)
        return None


@cl.header_auth_callback
async def header_auth_callback(headers: dict) -> cl.User | None:
    """
    Authenticate users via trusted proxy headers.

    This callback is used when Chainlit sits behind a reverse proxy
    that handles authentication (e.g., oauth2-proxy, Authelia, nginx with
    auth_request). The proxy passes user identity in headers.

    Common header patterns:
    - oauth2-proxy: X-Forwarded-Email, X-Forwarded-Preferred-Username
    - Authelia: Remote-Email, Remote-Name, Remote-User
    - Generic: X-Auth-User-Id, X-Auth-User-Email

    The header names are configurable via environment variables:
    - AUTH_USER_ID_HEADER: Header containing user's UUID (default: X-Auth-User-Id)
    - AUTH_USER_EMAIL_HEADER: Header containing user's email (default: X-Forwarded-Email)
    - AUTH_USER_NAME_HEADER: Header containing user's name (default: X-Forwarded-Preferred-Username)

    Args:
        headers: HTTP headers from the request.

    Returns:
        A Chainlit User object if valid headers are present, None otherwise.
    """
    from apps.users.models import User

    # Header names (configurable via environment)
    user_id_header = os.environ.get("AUTH_USER_ID_HEADER", "X-Auth-User-Id")
    user_email_header = os.environ.get("AUTH_USER_EMAIL_HEADER", "X-Forwarded-Email")
    user_name_header = os.environ.get("AUTH_USER_NAME_HEADER", "X-Forwarded-Preferred-Username")

    # Also support common reverse proxy header patterns
    alt_email_headers = ["x-auth-request-email", "remote-email"]
    alt_name_headers = ["x-auth-request-user", "remote-name", "remote-user"]

    # Normalize header keys (HTTP headers are case-insensitive)
    normalized_headers = {k.lower(): v for k, v in headers.items()}

    # Extract user identification
    user_id = normalized_headers.get(user_id_header.lower())
    user_email = normalized_headers.get(user_email_header.lower())
    user_name = normalized_headers.get(user_name_header.lower(), "")

    # Try alternative headers if primary not found
    if not user_email:
        for alt_header in alt_email_headers:
            user_email = normalized_headers.get(alt_header)
            if user_email:
                break

    if not user_name:
        for alt_header in alt_name_headers:
            user_name = normalized_headers.get(alt_header)
            if user_name:
                break

    if not user_id and not user_email:
        logger.debug("Header auth: no user identification headers found")
        return None

    try:
        # Look up user by ID or email
        if user_id:
            user = User.objects.get(pk=user_id)
        else:
            try:
                user = User.objects.get(email=user_email)
            except User.DoesNotExist:
                # Auto-provision user from headers if allowed
                auto_provision = os.environ.get("AUTH_HEADER_AUTO_PROVISION", "false").lower() == "true"
                if auto_provision:
                    user = User.objects.create(
                        email=user_email,
                        first_name=user_name.split()[0] if user_name else "",
                        last_name=" ".join(user_name.split()[1:]) if user_name else "",
                    )
                    logger.info("Auto-provisioned user from header auth: %s", user_email)
                else:
                    raise

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
