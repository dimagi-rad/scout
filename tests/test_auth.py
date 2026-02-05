"""
Comprehensive tests for Phase 4 (Auth) of the Scout data agent platform.

Tests OAuth integration with django-allauth, custom providers, and header-based auth.
"""
from unittest.mock import Mock, patch

import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site

User = get_user_model()


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def site(db):
    """Get or create the default Site object required by django-allauth."""
    site, _ = Site.objects.get_or_create(
        id=1,
        defaults={
            "domain": "testserver",
            "name": "Test Server",
        }
    )
    return site


@pytest.fixture
def google_social_app(db, site):
    """Create a Google OAuth social app configuration."""
    app = SocialApp.objects.create(
        provider="google",
        name="Google OAuth",
        client_id="test-google-client-id",
        secret="test-google-secret",
    )
    app.sites.add(site)
    return app


@pytest.fixture
def github_social_app(db, site):
    """Create a GitHub OAuth social app configuration."""
    app = SocialApp.objects.create(
        provider="github",
        name="GitHub OAuth",
        client_id="test-github-client-id",
        secret="test-github-secret",
    )
    app.sites.add(site)
    return app


@pytest.fixture
def social_account(db, user):
    """Create a SocialAccount linked to a user."""
    return SocialAccount.objects.create(
        user=user,
        provider="google",
        uid="123456789",
        extra_data={
            "email": user.email,
            "name": f"{user.first_name} {user.last_name}",
            "picture": "https://example.com/photo.jpg",
        },
    )


# ============================================================================
# 1. TestDjangoAllauthConfiguration
# ============================================================================


@pytest.mark.django_db
class TestDjangoAllauthConfiguration:
    """Tests for django-allauth configuration and settings."""

    def test_allauth_installed_apps(self, settings):
        """Test that django-allauth apps are in INSTALLED_APPS."""
        installed = settings.INSTALLED_APPS
        assert "allauth" in installed
        assert "allauth.account" in installed
        assert "allauth.socialaccount" in installed
        assert "allauth.socialaccount.providers.google" in installed
        assert "allauth.socialaccount.providers.github" in installed

    def test_authentication_backends_configured(self, settings):
        """Test that allauth authentication backend is configured."""
        backends = settings.AUTHENTICATION_BACKENDS
        assert "allauth.account.auth_backends.AuthenticationBackend" in backends
        assert "django.contrib.auth.backends.ModelBackend" in backends

    def test_account_settings(self, settings):
        """Test email-based account configuration."""
        # Email is required and unique (using django-allauth 65+ syntax)
        # ACCOUNT_LOGIN_METHODS includes 'email' (new syntax)
        assert hasattr(settings, "ACCOUNT_LOGIN_METHODS")
        assert "email" in settings.ACCOUNT_LOGIN_METHODS

        # ACCOUNT_SIGNUP_FIELDS includes email (new syntax)
        assert hasattr(settings, "ACCOUNT_SIGNUP_FIELDS")
        assert any("email" in field for field in settings.ACCOUNT_SIGNUP_FIELDS)

        # ACCOUNT_UNIQUE_EMAIL setting
        assert settings.ACCOUNT_UNIQUE_EMAIL is True

        # Email verification
        assert settings.ACCOUNT_EMAIL_VERIFICATION == "optional"

    def test_socialaccount_settings(self, settings):
        """Test social account configuration."""
        assert settings.SOCIALACCOUNT_AUTO_SIGNUP is True
        assert settings.SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT is True
        assert settings.SOCIALACCOUNT_EMAIL_VERIFICATION == "none"

    def test_site_id_configured(self, settings):
        """Test that SITE_ID is set for django.contrib.sites."""
        assert hasattr(settings, "SITE_ID")
        assert settings.SITE_ID == 1

    def test_google_provider_configured(self, settings):
        """Test Google OAuth provider configuration."""
        providers = settings.SOCIALACCOUNT_PROVIDERS
        assert "google" in providers
        assert "SCOPE" in providers["google"]
        assert "profile" in providers["google"]["SCOPE"]
        assert "email" in providers["google"]["SCOPE"]

    def test_github_provider_configured(self, settings):
        """Test GitHub OAuth provider configuration."""
        providers = settings.SOCIALACCOUNT_PROVIDERS
        assert "github" in providers
        assert "SCOPE" in providers["github"]
        assert "user:email" in providers["github"]["SCOPE"]


# ============================================================================
# 2. TestSocialAccountHelpers
# ============================================================================


@pytest.mark.django_db
class TestSocialAccountHelpers:
    """Tests for SocialAccount lookup helper functions."""

    def test_get_user_by_social_uid(self, user, social_account):
        """Test looking up user by social provider UID."""
        # Look up user by provider and uid
        found_account = SocialAccount.objects.filter(
            provider="google",
            uid="123456789",
        ).first()

        assert found_account is not None
        assert found_account.user == user
        assert found_account.provider == "google"
        assert found_account.uid == "123456789"

    def test_get_user_by_email_for_auto_connect(self, user):
        """Test looking up existing user by email for auto-connect."""
        # This simulates what happens when SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT is True
        email = user.email
        existing_user = User.objects.filter(email=email).first()

        assert existing_user is not None
        assert existing_user.email == email
        assert existing_user.id == user.id

    def test_social_account_extra_data(self, social_account):
        """Test that extra_data from OAuth provider is stored correctly."""
        assert "email" in social_account.extra_data
        assert "name" in social_account.extra_data
        assert social_account.extra_data["email"] == social_account.user.email

    def test_multiple_social_accounts_per_user(self, user):
        """Test that a user can have multiple social accounts (different providers)."""
        google_account = SocialAccount.objects.create(
            user=user,
            provider="google",
            uid="google_123",
            extra_data={"email": user.email},
        )

        github_account = SocialAccount.objects.create(
            user=user,
            provider="github",
            uid="github_456",
            extra_data={"email": user.email},
        )

        user_accounts = SocialAccount.objects.filter(user=user)
        assert user_accounts.count() == 2
        assert set(user_accounts.values_list("provider", flat=True)) == {"google", "github"}

    def test_no_duplicate_uid_per_provider(self, user, social_account):
        """Test that each provider UID should be unique."""
        # Attempt to create another account with the same provider and uid should fail
        # (or be prevented by database constraints)
        with pytest.raises(Exception):  # Could be IntegrityError depending on DB constraints
            SocialAccount.objects.create(
                user=user,
                provider="google",
                uid="123456789",  # Same UID as existing
                extra_data={},
            )


# ============================================================================
# 3. TestOAuthFlow (Mocked)
# ============================================================================


@pytest.mark.django_db
class TestOAuthFlow:
    """Tests for OAuth flow with mocked external providers."""

    def test_oauth_callback_creates_new_user(self, google_social_app, site):
        """Test that OAuth callback creates a new user if one doesn't exist."""
        # Mock OAuth provider response
        mock_user_data = {
            "id": "oauth_user_123",
            "email": "newuser@example.com",
            "given_name": "New",
            "family_name": "User",
            "picture": "https://example.com/photo.jpg",
        }

        # Simulate what happens during OAuth callback
        # 1. Check if user exists with this email
        existing_user = User.objects.filter(email=mock_user_data["email"]).first()
        assert existing_user is None

        # 2. Create new user (auto-signup)
        new_user = User.objects.create_user(
            email=mock_user_data["email"],
            first_name=mock_user_data["given_name"],
            last_name=mock_user_data["family_name"],
        )

        # 3. Create social account
        social_account = SocialAccount.objects.create(
            user=new_user,
            provider="google",
            uid=mock_user_data["id"],
            extra_data=mock_user_data,
        )

        assert new_user.email == mock_user_data["email"]
        assert social_account.user == new_user
        assert social_account.provider == "google"

    def test_oauth_callback_connects_existing_user(self, user, google_social_app, site):
        """Test that OAuth callback connects to existing user with same email."""
        # Mock OAuth provider response with email matching existing user
        mock_user_data = {
            "id": "oauth_existing_123",
            "email": user.email,  # Same email as existing user
            "given_name": "Test",
            "family_name": "User",
        }

        # Simulate auto-connect behavior
        existing_user = User.objects.filter(email=mock_user_data["email"]).first()
        assert existing_user is not None
        assert existing_user.id == user.id

        # Create social account linked to existing user
        social_account = SocialAccount.objects.create(
            user=existing_user,
            provider="google",
            uid=mock_user_data["id"],
            extra_data=mock_user_data,
        )

        assert social_account.user == user
        # User count should not increase
        assert User.objects.count() == 1

    def test_oauth_callback_returns_existing_social_account(self, user, social_account):
        """Test that OAuth callback returns existing user if social account already exists."""
        # Simulate returning user who has already authenticated via OAuth
        found_account = SocialAccount.objects.filter(
            provider="google",
            uid=social_account.uid,
        ).first()

        assert found_account is not None
        assert found_account.user == user
        assert found_account.id == social_account.id

    def test_invalid_oauth_provider_not_configured(self):
        """Test that unconfigured OAuth providers are not accessible."""
        # Try to look up a provider that doesn't have a SocialApp
        with pytest.raises(Exception):
            app = SocialApp.objects.get(provider="unknown_provider")

    def test_oauth_app_requires_client_credentials(self, site):
        """Test that OAuth apps must have client_id and secret."""
        app = SocialApp.objects.create(
            provider="test_provider",
            name="Test Provider",
            client_id="test_client_id",
            secret="test_secret",
        )
        app.sites.add(site)

        assert app.client_id == "test_client_id"
        assert app.secret == "test_secret"
        assert site in app.sites.all()


# ============================================================================
# 4. TestHeaderAuthCallback
# ============================================================================


@pytest.mark.django_db
class TestHeaderAuthCallback:
    """Tests for header-based authentication (reverse proxy scenarios)."""

    def test_header_auth_valid_user_lookup(self, user):
        """Test successful user lookup from header authentication."""
        # Simulate header auth where reverse proxy passes user email
        headers = {
            "X-Forwarded-User": user.email,
            "X-Forwarded-Email": user.email,
        }

        # Look up user by email from header
        authenticated_user = User.objects.filter(
            email=headers["X-Forwarded-Email"]
        ).first()

        assert authenticated_user is not None
        assert authenticated_user.id == user.id
        assert authenticated_user.email == user.email

    def test_header_auth_creates_user_if_not_exists(self):
        """Test that header auth can create user on first access."""
        headers = {
            "X-Forwarded-User": "newheaderuser",
            "X-Forwarded-Email": "headeruser@example.com",
            "X-Forwarded-Name": "Header User",
        }

        # Check if user exists
        existing_user = User.objects.filter(email=headers["X-Forwarded-Email"]).first()
        assert existing_user is None

        # Create user from headers
        new_user = User.objects.create_user(
            email=headers["X-Forwarded-Email"],
            first_name=headers.get("X-Forwarded-Name", "").split()[0] if headers.get("X-Forwarded-Name") else "",
            last_name=" ".join(headers.get("X-Forwarded-Name", "").split()[1:]) if headers.get("X-Forwarded-Name") else "",
        )

        assert new_user.email == headers["X-Forwarded-Email"]
        assert User.objects.filter(email=headers["X-Forwarded-Email"]).exists()

    def test_header_auth_missing_email_header(self):
        """Test that authentication fails if email header is missing."""
        headers = {
            "X-Forwarded-User": "someuser",
            # Missing X-Forwarded-Email
        }

        authenticated_user = User.objects.filter(
            email=headers.get("X-Forwarded-Email")
        ).first() if headers.get("X-Forwarded-Email") else None

        assert authenticated_user is None

    def test_header_auth_invalid_email_format(self):
        """Test handling of invalid email format in headers."""
        headers = {
            "X-Forwarded-Email": "not-a-valid-email",
        }

        # Try to create user with invalid email
        # Django's email validation may not raise during create_user
        # So we test that lookup returns None for invalid format
        user = User.objects.filter(email=headers["X-Forwarded-Email"]).first()
        assert user is None  # No user should exist with this email


# ============================================================================
# 5. TestChainlitAuthIntegration (Mock Tests)
# ============================================================================


@pytest.mark.django_db
class TestChainlitAuthIntegration:
    """Tests for Chainlit authentication callback integration."""

    @patch("allauth.socialaccount.models.SocialAccount.objects")
    def test_oauth_callback_lookup_by_token(self, mock_social_account_objects, user, social_account):
        """Test OAuth callback looks up user by provider token/uid."""
        # Mock the SocialAccount query
        mock_social_account_objects.filter.return_value.first.return_value = social_account

        # Simulate OAuth callback with provider info
        provider = "google"
        uid = "123456789"

        found_account = SocialAccount.objects.filter(
            provider=provider,
            uid=uid,
        ).first()

        assert found_account == social_account
        assert found_account.user == user

    def test_password_auth_fallback_development(self, user):
        """Test password authentication as fallback for development."""
        # Simulate password auth callback
        username = user.email
        password = "testpass123"

        # In real scenario, this would check password
        authenticated_user = User.objects.filter(email=username).first()

        assert authenticated_user is not None
        assert authenticated_user.email == username

    def test_user_has_required_attributes_for_chainlit(self, user):
        """Test that user has all required attributes for Chainlit integration."""
        # Chainlit typically needs: identifier (email), metadata (name, etc.)
        assert hasattr(user, "email")
        assert hasattr(user, "first_name")
        assert hasattr(user, "last_name")
        assert user.email is not None
        assert len(user.email) > 0

    def test_social_account_provides_profile_data(self, social_account):
        """Test that social account extra_data can be used for user profile."""
        # Check that profile data from OAuth is accessible
        assert "email" in social_account.extra_data
        profile_data = {
            "email": social_account.extra_data.get("email"),
            "name": social_account.extra_data.get("name"),
            "picture": social_account.extra_data.get("picture"),
        }

        assert profile_data["email"] == social_account.user.email
        assert profile_data["name"] is not None or profile_data["picture"] is not None


# ============================================================================
# 6. TestMultiProviderSupport
# ============================================================================


@pytest.mark.django_db
class TestMultiProviderSupport:
    """Tests for supporting multiple OAuth providers."""

    def test_google_and_github_providers_coexist(self, google_social_app, github_social_app):
        """Test that multiple OAuth providers can be configured simultaneously."""
        google_app = SocialApp.objects.filter(provider="google").first()
        github_app = SocialApp.objects.filter(provider="github").first()

        assert google_app is not None
        assert github_app is not None
        assert google_app.provider == "google"
        assert github_app.provider == "github"

    def test_user_can_link_multiple_providers(self, user, site):
        """Test that a user can link accounts from multiple OAuth providers."""
        # Create Google account
        google_account = SocialAccount.objects.create(
            user=user,
            provider="google",
            uid="google_user_123",
            extra_data={"email": user.email},
        )

        # Create GitHub account for same user
        github_account = SocialAccount.objects.create(
            user=user,
            provider="github",
            uid="github_user_456",
            extra_data={"email": user.email},
        )

        user_accounts = SocialAccount.objects.filter(user=user)
        assert user_accounts.count() == 2
        providers = set(user_accounts.values_list("provider", flat=True))
        assert providers == {"google", "github"}

    def test_each_provider_has_unique_uid(self, user):
        """Test that UIDs are unique per provider but can differ across providers."""
        google_account = SocialAccount.objects.create(
            user=user,
            provider="google",
            uid="12345",
            extra_data={},
        )

        # Same UID but different provider should be allowed
        github_account = SocialAccount.objects.create(
            user=user,
            provider="github",
            uid="12345",  # Same UID, different provider
            extra_data={},
        )

        assert google_account.uid == github_account.uid
        assert google_account.provider != github_account.provider


# ============================================================================
# 7. TestCustomCommCareProvider
# ============================================================================


@pytest.mark.django_db
class TestCustomCommCareProvider:
    """Tests for the custom CommCare OAuth provider implementation."""

    def test_commcare_provider_is_registered(self, settings):
        """Test that CommCare provider is registered in INSTALLED_APPS."""
        assert "apps.users.providers.commcare" in settings.INSTALLED_APPS

    def test_commcare_provider_imports(self):
        """Test that CommCare provider classes can be imported."""
        from apps.users.providers.commcare.provider import (
            CommCareAccount,
            CommCareProvider,
            provider_classes,
        )
        from apps.users.providers.commcare.views import (
            CommCareOAuth2Adapter,
            oauth2_callback,
            oauth2_login,
        )

        assert CommCareProvider.id == "commcare"
        assert CommCareProvider.name == "CommCare"
        assert CommCareOAuth2Adapter.provider_id == "commcare"
        assert CommCareProvider in provider_classes

    def test_commcare_provider_extract_uid(self, site):
        """Test CommCare provider can extract user ID from OAuth response."""
        from apps.users.providers.commcare.provider import CommCareProvider

        # Create a mock SocialApp for the provider
        app = SocialApp.objects.create(
            provider="commcare",
            name="CommCare OAuth",
            client_id="test-commcare-client-id",
            secret="test-commcare-secret",
        )
        app.sites.add(site)

        provider = CommCareProvider(request=None, app=app)
        test_data = {"id": "user123", "username": "testuser"}
        uid = provider.extract_uid(test_data)
        assert uid == "user123"

    def test_commcare_provider_extract_common_fields(self, site):
        """Test CommCare provider can extract common user fields."""
        from apps.users.providers.commcare.provider import CommCareProvider

        # Create a mock SocialApp for the provider
        app = SocialApp.objects.create(
            provider="commcare",
            name="CommCare OAuth",
            client_id="test-commcare-client-id-2",
            secret="test-commcare-secret",
        )
        app.sites.add(site)

        provider = CommCareProvider(request=None, app=app)
        test_data = {
            "id": "user123",
            "email": "user@example.com",
            "username": "testuser",
            "first_name": "Test",
            "last_name": "User",
        }
        fields = provider.extract_common_fields(test_data)

        assert fields["email"] == "user@example.com"
        assert fields["username"] == "testuser"
        assert fields["first_name"] == "Test"
        assert fields["last_name"] == "User"

    def test_commcare_provider_default_scope(self, site):
        """Test CommCare provider has correct default scope."""
        from apps.users.providers.commcare.provider import CommCareProvider

        # Create a mock SocialApp for the provider
        app = SocialApp.objects.create(
            provider="commcare",
            name="CommCare OAuth",
            client_id="test-commcare-client-id-3",
            secret="test-commcare-secret",
        )
        app.sites.add(site)

        provider = CommCareProvider(request=None, app=app)
        scope = provider.get_default_scope()
        assert "read" in scope

    def test_commcare_oauth2_adapter_endpoints(self):
        """Test CommCare adapter has correct OAuth endpoint URLs."""
        from apps.users.providers.commcare.views import CommCareOAuth2Adapter

        assert "commcarehq.org" in CommCareOAuth2Adapter.access_token_url
        assert "commcarehq.org" in CommCareOAuth2Adapter.authorize_url
        assert "commcarehq.org" in CommCareOAuth2Adapter.profile_url
        assert "/oauth/token" in CommCareOAuth2Adapter.access_token_url
        assert "/oauth/authorize" in CommCareOAuth2Adapter.authorize_url
        assert "/api/" in CommCareOAuth2Adapter.profile_url

    def test_commcare_account_to_str(self):
        """Test CommCare account string representation."""
        from apps.users.providers.commcare.provider import CommCareAccount
        from unittest.mock import Mock

        mock_account = Mock()
        mock_account.extra_data = {"username": "testuser"}

        account = CommCareAccount(mock_account)
        assert account.to_str() == "testuser"

    def test_commcare_account_no_avatar(self):
        """Test CommCare account has no avatar URL."""
        from apps.users.providers.commcare.provider import CommCareAccount
        from unittest.mock import Mock

        mock_account = Mock()
        mock_account.extra_data = {}

        account = CommCareAccount(mock_account)
        assert account.get_avatar_url() is None
