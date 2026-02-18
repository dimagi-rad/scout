"""
Pytest configuration and fixtures for Scout tests.
"""
import pytest
from django.contrib.auth import get_user_model

from apps.projects.models import DatabaseConnection


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
def tenant_membership(user):
    from apps.users.models import TenantMembership

    return TenantMembership.objects.create(
        user=user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
    )


@pytest.fixture
def db_connection(db, user):
    """Create a DatabaseConnection for tests."""
    conn = DatabaseConnection(
        name="Test Connection",
        db_host="localhost",
        db_port=5432,
        db_name="testdb",
        created_by=user,
    )
    conn.db_user = "testuser"
    conn.db_password = "testpass"
    conn.save()
    return conn
