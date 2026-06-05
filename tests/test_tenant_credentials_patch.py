import json
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.users.adapters import decrypt_credential, encrypt_credential
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.api_key_providers import CredentialVerificationError


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(email="u@example.com", password="pw")


def _make_ocs_membership(user):
    """Create an OCS chatbot membership backed by an API-key TenantConnection.

    Returns ``(membership, connection)``. The connection is what the PATCH
    endpoint addresses; the membership links to it so the view can sample a
    tenant to re-verify the rotated key against.
    """
    tenant = Tenant.objects.create(provider="ocs", external_id="exp-1", canonical_name="Bot One")
    conn = TenantConnection.objects.create(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("old_ocs_key"),
    )
    tm = TenantMembership.objects.create(user=user, tenant=tenant, connection=conn)
    return tm, conn


def test_patch_ocs_rotates_key(user):
    _tm, conn = _make_ocs_membership(user)
    client = Client()
    client.force_login(user)
    with patch(
        "apps.users.services.api_key_providers.ocs.OCSStrategy.verify_for_tenant",
        new_callable=AsyncMock,
        return_value=None,
    ):
        resp = client.patch(
            f"/api/auth/connections/{conn.id}/",
            data=json.dumps({"fields": {"api_key": "new_ocs_key"}}),
            content_type="application/json",
        )
    assert resp.status_code == 200

    conn.refresh_from_db()
    assert decrypt_credential(conn.encrypted_credential) == "new_ocs_key"


def test_patch_ocs_rejects_invalid_key(user):
    _tm, conn = _make_ocs_membership(user)
    client = Client()
    client.force_login(user)
    with patch(
        "apps.users.services.api_key_providers.ocs.OCSStrategy.verify_for_tenant",
        new_callable=AsyncMock,
        side_effect=CredentialVerificationError("revoked"),
    ):
        resp = client.patch(
            f"/api/auth/connections/{conn.id}/",
            data=json.dumps({"fields": {"api_key": "bad"}}),
            content_type="application/json",
        )
    assert resp.status_code == 400


def test_patch_missing_required_editable_field_returns_400(user):
    _tm, conn = _make_ocs_membership(user)
    client = Client()
    client.force_login(user)
    resp = client.patch(
        f"/api/auth/connections/{conn.id}/",
        data=json.dumps({"fields": {}}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "api_key" in resp.json()["error"]
