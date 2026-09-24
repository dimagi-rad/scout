"""The explicit verification retry endpoint stays reachable while access is denied."""

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.users.models import TenantMembership
from apps.workspaces import access
from apps.workspaces.access import resolve_workspace_access_ex
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from apps.workspaces.services.access_freshness import VERIFICATION_UNAVAILABLE
from tests.upstream_proofs import grant_fresh_upstream_access, make_proof_stale


@pytest.fixture(autouse=True)
def _clear_retry_cooldowns():
    cache.clear()
    yield
    cache.clear()


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
    assert second.json()["reason"] == first.json()["reason"] == "upstream_access_lost"
    assert second.json()["retryable"] is False
    assert len(upstream_provider.requests) == 1


@pytest.mark.django_db(transaction=True)
def test_a_retry_that_observes_revocation_names_it(user, workspace, tenant, upstream_provider):
    make_proof_stale(user, tenant)
    upstream_provider.domains = []

    response = _retry(user, workspace)

    assert response.json()["reason"] == "upstream_access_lost"
    assert response.json()["lost_tenants"] == [tenant.canonical_name]


@pytest.mark.django_db(transaction=True)
def test_a_granted_retry_with_a_tombstone_stays_throttled(
    settings, user, workspace, tenant, upstream_provider
):
    # A granted retry beside a tombstoned tenant exists only under any-of; all-of
    # denies the workspace for the archived source (tests/test_workspace_all_of_access.py).
    settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = False
    second = type(tenant).objects.create(
        provider="commcare", external_id="archived-domain", canonical_name="Archived"
    )
    WorkspaceTenant.objects.create(workspace=workspace, tenant=second)
    grant_fresh_upstream_access(user, second)
    TenantMembership.objects.filter(user=user, tenant=second).update(archived_at=timezone.now())
    upstream_provider.domains = [tenant.external_id]

    assert _retry(user, workspace).status_code == 200
    assert _retry(user, workspace).status_code == 200
    assert len(upstream_provider.requests) == 1


@pytest.mark.django_db(transaction=True)
def test_a_cached_reason_is_not_replayed_for_another_workspace(
    user, workspace, tenant, upstream_provider
):
    other = Workspace.objects.create(name="Other", created_by=user)
    WorkspaceMembership.objects.create(workspace=other, user=user, role=WorkspaceRole.MANAGE)
    other_tenant = type(tenant).objects.create(
        provider="commcare", external_id="other-domain", canonical_name="Other"
    )
    WorkspaceTenant.objects.create(workspace=other, tenant=other_tenant)
    grant_fresh_upstream_access(user, other_tenant)
    make_proof_stale(user, other_tenant)
    TenantMembership.objects.filter(user=user, tenant=tenant).update(archived_at=timezone.now())
    upstream_provider.domains = []

    first = _retry(user, workspace)
    second = _retry(user, other)

    assert first.json()["reason"] == "upstream_access_lost"
    assert second.json()["reason"] == "verification_in_progress"
