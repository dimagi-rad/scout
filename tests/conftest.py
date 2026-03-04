"""
Pytest configuration and fixtures for Scout tests.
"""

import pytest
from django.contrib.auth import get_user_model


@pytest.fixture
def user(db):
    """Create a test user."""
    User = get_user_model()
    return User.objects.create_user(
        email="test@example.com",
        password="testpass123",
        first_name="Test",
        last_name="User",
    )


@pytest.fixture
def admin_user(db):
    """Create a test admin user."""
    User = get_user_model()
    return User.objects.create_superuser(
        email="admin@example.com",
        password="adminpass123",
        first_name="Admin",
        last_name="User",
    )


@pytest.fixture
def tenant(db):
    from apps.users.models import Tenant

    return Tenant.objects.create(
        provider="commcare", external_id="test-domain", canonical_name="Test Domain"
    )


@pytest.fixture
def tenant_membership(db, user, tenant):
    from apps.users.models import TenantMembership

    return TenantMembership.objects.create(user=user, tenant=tenant)


@pytest.fixture
def other_user(db):
    User = get_user_model()
    return User.objects.create_user(
        email="other@example.com",
        password="otherpass123",
    )


@pytest.fixture
def workspace(db, tenant):
    from apps.projects.models import TenantWorkspace

    return TenantWorkspace.objects.create(tenant=tenant)
