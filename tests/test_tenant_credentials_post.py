import json
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model

from apps.users.services.api_key_providers import (
    CredentialVerificationError,
    TenantDescriptor,
)


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(email="u@example.com", password="pw")


def _post(client, body):
    return client.post(
        "/api/auth/tenant-credentials/",
        data=json.dumps(body),
        content_type="application/json",
    )


def test_commcare_post_returns_single_membership(client, user):
    client.force_login(user)
    with patch(
        "apps.users.services.api_key_providers.commcare.CommCareStrategy.verify_and_discover",
        new_callable=AsyncMock,
        return_value=[TenantDescriptor("dimagi", "dimagi")],
    ):
        resp = _post(
            client,
            {
                "provider": "commcare",
                "fields": {"domain": "dimagi", "username": "u", "api_key": "k"},
            },
        )
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert len(body["memberships"]) == 1
    m = body["memberships"][0]
    assert m["tenant_id"] == "dimagi"
    assert m["tenant_name"] == "dimagi"


def test_ocs_post_returns_multiple_memberships(client, user):
    client.force_login(user)
    descriptors = [
        TenantDescriptor("exp-1", "Bot One"),
        TenantDescriptor("exp-2", "Bot Two"),
    ]
    with patch(
        "apps.users.services.api_key_providers.ocs.OCSStrategy.verify_and_discover",
        new_callable=AsyncMock,
        return_value=descriptors,
    ):
        resp = _post(
            client,
            {"provider": "ocs", "fields": {"api_key": "ocs_xxx"}},
        )
    assert resp.status_code == 201, resp.content
    memberships = resp.json()["memberships"]
    assert {m["tenant_id"] for m in memberships} == {"exp-1", "exp-2"}

    from apps.users.models import Tenant, TenantCredential, TenantMembership

    assert Tenant.objects.filter(provider="ocs").count() == 2
    assert TenantMembership.objects.filter(user=user, tenant__provider="ocs").count() == 2
    creds = TenantCredential.objects.filter(tenant_membership__user=user)
    assert {c.encrypted_credential for c in creds}  # non-empty
    assert all(c.credential_type == TenantCredential.API_KEY for c in creds)


def test_unknown_provider_returns_400(client, user):
    client.force_login(user)
    resp = _post(client, {"provider": "fake", "fields": {}})
    assert resp.status_code == 400
    assert "fake" in resp.json()["error"].lower()


def test_missing_required_field_returns_400(client, user):
    client.force_login(user)
    resp = _post(client, {"provider": "ocs", "fields": {}})
    assert resp.status_code == 400
    assert "api_key" in resp.json()["error"]


def test_verification_error_returns_400(client, user):
    client.force_login(user)
    with patch(
        "apps.users.services.api_key_providers.ocs.OCSStrategy.verify_and_discover",
        new_callable=AsyncMock,
        side_effect=CredentialVerificationError("nope"),
    ):
        resp = _post(client, {"provider": "ocs", "fields": {"api_key": "bad"}})
    assert resp.status_code == 400
    assert "nope" in resp.json()["error"]


def test_partial_failure_is_atomic(client, user):
    """If membership creation fails partway, no rows are persisted."""
    client.force_login(user)
    descriptors = [
        TenantDescriptor("exp-1", "Bot One"),
        TenantDescriptor("exp-2", "Bot Two"),
    ]

    # Inject failure inside the persistence loop on the second descriptor by
    # making Tenant.objects.get_or_create raise on its second call. This
    # exercises the atomic rollback path.
    from apps.users.models import Tenant

    real_get_or_create = Tenant.objects.get_or_create
    calls = {"n": 0}

    def flaky_get_or_create(**kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("boom")
        return real_get_or_create(**kwargs)

    with (
        patch(
            "apps.users.services.api_key_providers.ocs.OCSStrategy.verify_and_discover",
            new_callable=AsyncMock,
            return_value=descriptors,
        ),
        patch.object(Tenant.objects, "get_or_create", side_effect=flaky_get_or_create),
    ):
        resp = _post(client, {"provider": "ocs", "fields": {"api_key": "k"}})
    assert resp.status_code == 500
    from apps.users.models import TenantMembership

    assert not TenantMembership.objects.filter(user=user).exists()
