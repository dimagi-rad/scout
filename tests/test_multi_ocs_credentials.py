"""Tests for multi-team OCS credential support."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.users.adapters import encrypt_credential
from apps.users.models import Tenant, TenantCredential, TenantMembership
from apps.users.services.credential_resolver import aresolve_credential


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_multiple_api_key_credentials_per_membership(user):
    """Test that multiple API-key credentials can be stored for same membership."""
    # Create a tenant
    tenant = await Tenant.objects.acreate(
        provider="ocs",
        external_id="exp-1",
        canonical_name="Test Experiment",
    )

    # Create a membership
    tm = await TenantMembership.objects.acreate(user=user, tenant=tenant)

    # Add two API key credentials with different team IDs
    await TenantCredential.objects.acreate(
        tenant_membership=tm,
        credential_type=TenantCredential.API_KEY,
        encrypted_credential="encrypted_key_team_a",
        team_id="team_a",
        team_name="Team A Workspace",
    )

    await TenantCredential.objects.acreate(
        tenant_membership=tm,
        credential_type=TenantCredential.API_KEY,
        encrypted_credential="encrypted_key_team_b",
        team_id="team_b",
        team_name="Team B Workspace",
    )

    # Verify both credentials exist
    creds = [c async for c in tm.credentials.all()]
    assert len(creds) == 2
    assert set(c.team_id for c in creds) == {"team_a", "team_b"}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resolve_credential_with_team_id(user, mocker):
    """Test credential resolution with team_id parameter."""
    # Create a tenant
    tenant = await Tenant.objects.acreate(
        provider="ocs",
        external_id="exp-1",
        canonical_name="Test Experiment",
    )

    # Create a membership
    tm = await TenantMembership.objects.acreate(user=user, tenant=tenant)

    # Add two API key credentials with different team IDs
    test_key_a = "api_key_team_a"
    test_key_b = "api_key_team_b"

    await TenantCredential.objects.acreate(
        tenant_membership=tm,
        credential_type=TenantCredential.API_KEY,
        encrypted_credential=encrypt_credential(test_key_a),
        team_id="team_a",
        team_name="Team A Workspace",
    )

    await TenantCredential.objects.acreate(
        tenant_membership=tm,
        credential_type=TenantCredential.API_KEY,
        encrypted_credential=encrypt_credential(test_key_b),
        team_id="team_b",
        team_name="Team B Workspace",
    )

    # Test async credential resolution with team_id
    cred_a = await aresolve_credential(tm, team_id="team_a")
    assert cred_a is not None
    assert cred_a["type"] == "api_key"
    assert cred_a["value"] == test_key_a

    cred_b = await aresolve_credential(tm, team_id="team_b")
    assert cred_b is not None
    assert cred_b["type"] == "api_key"
    assert cred_b["value"] == test_key_b

    # Test fallback when team_id doesn't exist
    cred_fallback = await aresolve_credential(tm, team_id="nonexistent_team")
    assert cred_fallback is not None  # Should fall back to first credential


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_oauth_credential_unique_per_membership(user):
    """Test that only one OAuth credential can exist per membership (team_id=NULL)."""
    # Create a tenant
    tenant = await Tenant.objects.acreate(
        provider="ocs",
        external_id="exp-1",
        canonical_name="Test Experiment",
    )

    # Create a membership
    tm = await TenantMembership.objects.acreate(user=user, tenant=tenant)

    # Add OAuth credential
    await TenantCredential.objects.acreate(
        tenant_membership=tm,
        credential_type=TenantCredential.OAUTH,
        team_id=None,
    )

    # Attempting to add another OAuth credential with team_id=NULL should fail
    # due to unique constraint
    with pytest.raises(IntegrityError):
        await TenantCredential.objects.acreate(
            tenant_membership=tm,
            credential_type=TenantCredential.OAUTH,
            team_id=None,
        )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mixed_oauth_and_api_key_credentials(user, mocker):
    """Test that OAuth and API-key credentials can coexist for same membership."""
    # Create a tenant
    tenant = await Tenant.objects.acreate(
        provider="ocs",
        external_id="exp-1",
        canonical_name="Test Experiment",
    )

    # Create a membership
    tm = await TenantMembership.objects.acreate(user=user, tenant=tenant)

    # Add OAuth credential
    await TenantCredential.objects.acreate(
        tenant_membership=tm,
        credential_type=TenantCredential.OAUTH,
        team_id=None,
    )

    # Add API key credential for Team A
    test_key_a = "api_key_team_a"
    await TenantCredential.objects.acreate(
        tenant_membership=tm,
        credential_type=TenantCredential.API_KEY,
        encrypted_credential=encrypt_credential(test_key_a),
        team_id="team_a",
        team_name="Team A Workspace",
    )

    # Verify we have 2 credentials
    creds = [c async for c in tm.credentials.all()]
    assert len(creds) == 2

    # Mock the SocialToken lookup for OAuth resolution
    # Create a token that doesn't need refresh
    future_time = timezone.now() + timedelta(hours=10)
    mock_token = MagicMock(token="oauth_token_value", expires_at=future_time)
    mock_qs = MagicMock()
    mock_qs.select_related = MagicMock(return_value=mock_qs)
    mock_qs.afirst = AsyncMock(return_value=mock_token)

    mocker.patch(
        "apps.users.services.credential_resolver._social_token_qs",
        return_value=mock_qs,
    )

    # OAuth credential should be resolved when no team_id specified
    cred_oauth = await aresolve_credential(tm, team_id=None)
    assert cred_oauth is not None
    assert cred_oauth["type"] == "oauth"

    # API key credential should be resolved with team_id
    cred_api = await aresolve_credential(tm, team_id="team_a")
    assert cred_api["type"] == "api_key"
    assert cred_api["value"] == test_key_a


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_credential_team_metadata(user):
    """Test that team_id and team_name are stored and retrieved correctly."""
    # Create a tenant
    tenant = await Tenant.objects.acreate(
        provider="ocs",
        external_id="exp-1",
        canonical_name="Test Experiment",
    )

    # Create a membership
    tm = await TenantMembership.objects.acreate(user=user, tenant=tenant)

    # Create credential with team metadata
    cred = await TenantCredential.objects.acreate(
        tenant_membership=tm,
        credential_type=TenantCredential.API_KEY,
        encrypted_credential="encrypted_key",
        team_id="team-uuid-123",
        team_name="My OCS Workspace",
    )

    # Retrieve and verify
    retrieved = await TenantCredential.objects.aget(id=cred.id)
    assert retrieved.team_id == "team-uuid-123"
    assert retrieved.team_name == "My OCS Workspace"
