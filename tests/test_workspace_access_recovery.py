"""The explicit verification retry endpoint stays reachable while access is denied."""

import pytest
from django.test import Client
from django.utils import timezone

from apps.users.models import TenantMembership
from apps.workspaces import access
from apps.workspaces.access import resolve_workspace_access_ex
from apps.workspaces.services.access_freshness import VERIFICATION_UNAVAILABLE
from tests.upstream_proofs import make_proof_stale


def _retry(user, workspace):
    client = Client()
    client.force_login(user)
    return client.post(f"/api/workspaces/{workspace.id}/access/verify/")


@pytest.mark.django_db(transaction=True)
def test_retry_reports_temporary_failure_then_restores_access(
    user, workspace, tenant, upstream_provider, monkeypatch
):
    monkeypatch.setattr(access, "RETRY_COOLDOWN_SECONDS", 0)
    make_proof_stale(user, tenant)
    upstream_provider.failure = 503

    failed = _retry(user, workspace)

    assert failed.status_code == 403
    assert failed.json()["reason"] == VERIFICATION_UNAVAILABLE
    assert failed.json()["retryable"] is True
    assert TenantMembership.objects.filter(user=user, tenant=tenant).exists()

    upstream_provider.failure = None
    upstream_provider.domains = [tenant.external_id]
    restored = _retry(user, workspace)

    assert restored.status_code == 200
    assert restored.json() == {"has_access": True}
    assert len(upstream_provider.requests) == 2


@pytest.mark.django_db(transaction=True)
def test_retry_restores_a_membership_archived_by_revocation(
    user, workspace, tenant, upstream_provider
):
    make_proof_stale(user, tenant)
    upstream_provider.domains = []
    assert not resolve_workspace_access_ex(user, workspace.id).granted
    assert not TenantMembership.objects.filter(user=user, tenant=tenant).exists()

    upstream_provider.domains = [tenant.external_id]
    response = _retry(user, workspace)

    assert response.status_code == 200
    assert TenantMembership.objects.filter(user=user, tenant=tenant).exists()


@pytest.mark.django_db(transaction=True)
def test_an_outage_during_retry_of_archived_access_stays_retryable(
    user, workspace, tenant, upstream_provider
):
    TenantMembership.objects.filter(user=user, tenant=tenant).update(archived_at=timezone.now())
    upstream_provider.failure = 503

    response = _retry(user, workspace)

    assert response.status_code == 403
    assert response.json()["reason"] == VERIFICATION_UNAVAILABLE
    assert response.json()["retryable"] is True


@pytest.mark.django_db(transaction=True)
def test_failing_retries_are_throttled(user, workspace, tenant, upstream_provider):
    make_proof_stale(user, tenant)
    upstream_provider.failure = 503

    first = _retry(user, workspace)
    second = _retry(user, workspace)

    assert first.json()["retryable"] is True
    assert second.json()["retryable"] is True
    assert len(upstream_provider.requests) == 1


@pytest.mark.django_db(transaction=True)
def test_retry_with_the_gate_off_never_calls_a_provider(
    user, workspace, tenant, upstream_provider, settings
):
    settings.UPSTREAM_ACCESS_FRESHNESS_ENFORCED = False
    make_proof_stale(user, tenant)
    upstream_provider.failure = 503

    response = _retry(user, workspace)

    assert response.status_code == 200
    assert upstream_provider.requests == []


@pytest.mark.django_db(transaction=True)
def test_retry_with_fresh_proof_makes_no_upstream_call(user, workspace, upstream_provider):
    response = _retry(user, workspace)

    assert response.status_code == 200
    assert upstream_provider.requests == []


@pytest.mark.django_db(transaction=True)
def test_outsider_gets_generic_denial_and_triggers_nothing(
    workspace, other_user, upstream_provider
):
    response = _retry(other_user, workspace)

    assert response.status_code == 403
    assert "reason" not in response.json()
    assert upstream_provider.requests == []


@pytest.mark.django_db(transaction=True)
def test_retry_requires_post(user, workspace):
    client = Client()
    client.force_login(user)

    assert client.get(f"/api/workspaces/{workspace.id}/access/verify/").status_code == 405


@pytest.mark.django_db(transaction=True)
def test_terminal_denials_are_throttled_too(user, workspace, tenant, upstream_provider):
    TenantMembership.objects.filter(user=user, tenant=tenant).update(archived_at=timezone.now())
    upstream_provider.domains = []

    first = _retry(user, workspace)
    second = _retry(user, workspace)

    assert first.status_code == 403
    assert second.status_code == 403
    assert len(upstream_provider.requests) == 1


@pytest.mark.django_db(transaction=True)
def test_a_retry_that_observes_revocation_names_it(user, workspace, tenant, upstream_provider):
    make_proof_stale(user, tenant)
    upstream_provider.domains = []

    response = _retry(user, workspace)

    assert response.json()["reason"] == "upstream_access_lost"
    assert response.json()["lost_tenants"] == [tenant.canonical_name]
