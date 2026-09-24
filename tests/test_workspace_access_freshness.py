"""The central authorizer enforces the five-minute upstream-freshness policy.

Every provider request lands in the ``upstream_provider`` stub, so an unexpected
call is counted rather than sent.
"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from django.db import transaction
from django.test import Client
from django.utils import timezone

from apps.users.models import TenantMembership, UpstreamAccessProof, VerificationControl
from apps.users.services.access_verification import PROOF_MAX_AGE, proofs_are_fresh
from apps.workspaces import access as access_module
from apps.workspaces.access import (
    INSUFFICIENT_ROLE,
    NOT_MEMBER,
    TENANT_ACCESS_LOST,
    aresolve_workspace_access_ex,
    resolve_workspace_access_ex,
)
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from apps.workspaces.services.access_freshness import (
    CREDENTIAL_MISSING,
    FRESHNESS_DENIAL_REASONS,
    UPSTREAM_ACCESS_LOST,
    VERIFICATION_IN_PROGRESS,
    VERIFICATION_UNAVAILABLE,
    VerificationBudget,
)
from tests.upstream_proofs import (
    amake_proof_stale,
    grant_fresh_upstream_access,
    make_proof_stale,
)


def _proof(user, tenant):
    return UpstreamAccessProof.objects.get(connection__user=user, tenant=tenant)


@pytest.mark.django_db(transaction=True)
def test_fresh_proof_admits_without_any_upstream_call(user, workspace, upstream_provider):
    result = resolve_workspace_access_ex(user, workspace.id)

    assert result.granted
    assert upstream_provider.requests == []


@pytest.mark.django_db(transaction=True)
def test_proof_expires_exactly_at_five_minutes(user, workspace, tenant):
    membership = TenantMembership.objects.get(user=user, tenant=tenant)
    now = timezone.now()

    def fresh_when_verified(verified_at):
        UpstreamAccessProof.objects.filter(connection__user=user, tenant=tenant).update(
            verified_at=verified_at
        )
        return proofs_are_fresh(user.id, membership.connection_id, {tenant.id}, now=now)

    assert fresh_when_verified(now - PROOF_MAX_AGE + timedelta(microseconds=1))
    assert not fresh_when_verified(now - PROOF_MAX_AGE)


@pytest.mark.django_db(transaction=True)
def test_expired_proof_is_rechecked_before_access(user, workspace, tenant, upstream_provider):
    UpstreamAccessProof.objects.filter(connection__user=user, tenant=tenant).update(
        verified_at=timezone.now() - PROOF_MAX_AGE
    )
    upstream_provider.domains = [tenant.external_id]

    result = resolve_workspace_access_ex(user, workspace.id)

    assert result.granted
    assert len(upstream_provider.requests) == 1


@pytest.mark.django_db(transaction=True)
def test_stale_proof_is_rechecked_once_and_refreshed(user, workspace, tenant, upstream_provider):
    make_proof_stale(user, tenant)
    upstream_provider.domains = [tenant.external_id]

    first = resolve_workspace_access_ex(user, workspace.id)
    second = resolve_workspace_access_ex(user, workspace.id)

    assert first.granted and second.granted
    assert len(upstream_provider.requests) == 1
    assert timezone.now() - _proof(user, tenant).verified_at < timedelta(minutes=1)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_concurrent_stale_requests_share_one_recheck(
    user, workspace, tenant, upstream_provider
):
    await amake_proof_stale(user, tenant)
    upstream_provider.domains = [tenant.external_id]
    upstream_provider.gate = asyncio.Event()

    first = asyncio.create_task(aresolve_workspace_access_ex(user, workspace.id))
    second = asyncio.create_task(aresolve_workspace_access_ex(user, workspace.id))
    try:
        await asyncio.wait_for(upstream_provider.requested.wait(), timeout=10)
        # Give the second caller time to find the lease held and start waiting on it.
        await asyncio.sleep(0.2)
        lease_held = await VerificationControl.objects.filter(
            connection__user=user, lease_token__isnull=False
        ).aexists()
        assert lease_held
        assert not first.done() and not second.done()
    finally:
        # Never leave the callers parked on the gate if an assertion above fails.
        upstream_provider.gate.set()
    results = await asyncio.gather(first, second)

    assert all(result.granted for result in results)
    assert len(upstream_provider.requests) == 1


@pytest.mark.django_db(transaction=True)
def test_authoritative_revocation_denies_and_archives(user, workspace, tenant, upstream_provider):
    make_proof_stale(user, tenant)
    upstream_provider.domains = []

    result = resolve_workspace_access_ex(user, workspace.id)

    assert not result.granted
    assert result.denied_reason == UPSTREAM_ACCESS_LOST
    assert result.lost_tenant_names == (tenant.canonical_name,)
    assert not result.retryable
    membership = TenantMembership.all_objects.get(user=user, tenant=tenant)
    assert membership.archived_at is not None
    assert WorkspaceMembership.objects.filter(workspace=workspace, user=user).exists()


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("provider down"), 503, 429],
    ids=["transport", "5xx", "rate-limited"],
)
@pytest.mark.django_db(transaction=True)
def test_provider_outage_is_a_temporary_denial_that_keeps_memberships(
    user, workspace, tenant, upstream_provider, failure
):
    make_proof_stale(user, tenant)
    upstream_provider.failure = failure

    denied = resolve_workspace_access_ex(user, workspace.id)

    assert not denied.granted
    assert denied.denied_reason == VERIFICATION_UNAVAILABLE
    assert denied.retryable
    assert TenantMembership.objects.filter(user=user, tenant=tenant).exists()

    upstream_provider.failure = None
    upstream_provider.domains = [tenant.external_id]
    recovered = resolve_workspace_access_ex(user, workspace.id)

    assert recovered.granted


@pytest.mark.django_db(transaction=True)
def test_outage_response_body_is_structured_and_retryable(
    user, workspace, tenant, upstream_provider
):
    make_proof_stale(user, tenant)
    upstream_provider.failure = 503
    client = Client()
    client.force_login(user)

    response = client.get(f"/api/workspaces/{workspace.id}/data-dictionary/")

    assert response.status_code == 403
    body = response.json()
    assert body["reason"] == VERIFICATION_UNAVAILABLE
    assert body["retryable"] is True
    assert body["recovery_url"] == "/settings/connections"


@pytest.mark.django_db(transaction=True)
def test_unbound_legacy_membership_needs_reconnect_not_a_provider_call(
    user, workspace, tenant, upstream_provider
):
    TenantMembership.objects.filter(user=user, tenant=tenant).update(connection=None)

    result = resolve_workspace_access_ex(user, workspace.id)

    assert result.denied_reason == CREDENTIAL_MISSING
    assert not result.retryable
    assert upstream_provider.requests == []


@pytest.mark.django_db(transaction=True)
def test_outsider_and_insufficient_role_are_denied_before_any_upstream_call(
    user, workspace, tenant, read_user, other_user, upstream_provider
):
    make_proof_stale(read_user, tenant)

    outsider = resolve_workspace_access_ex(other_user, workspace.id)
    reader = resolve_workspace_access_ex(
        read_user, workspace.id, minimum_role=WorkspaceRole.READ_WRITE
    )

    assert outsider.denied_reason == NOT_MEMBER
    assert reader.denied_reason == INSUFFICIENT_ROLE
    assert upstream_provider.requests == []


@pytest.mark.django_db(transaction=True)
def test_recovery_metadata_mode_skips_freshness(user, workspace, tenant, upstream_provider):
    make_proof_stale(user, tenant)
    upstream_provider.failure = 503

    result = resolve_workspace_access_ex(user, workspace.id, verification=None)

    assert result.granted
    assert upstream_provider.requests == []


@pytest.mark.django_db(transaction=True)
def test_every_live_workspace_tenant_must_be_fresh(user, workspace, tenant, upstream_provider):
    second = type(tenant).objects.create(
        provider="commcare", external_id="second-domain", canonical_name="Second"
    )
    WorkspaceTenant.objects.create(workspace=workspace, tenant=second)
    grant_fresh_upstream_access(user, second)
    make_proof_stale(user, second)
    upstream_provider.failure = 503

    result = resolve_workspace_access_ex(user, workspace.id)

    assert result.denied_reason == VERIFICATION_UNAVAILABLE


@pytest.mark.django_db(transaction=True)
def test_empty_workspace_needs_only_membership(user, upstream_provider):
    empty = Workspace.objects.create(name="Empty", created_by=user)
    WorkspaceMembership.objects.create(workspace=empty, user=user, role=WorkspaceRole.MANAGE)

    assert resolve_workspace_access_ex(user, empty.id).granted
    assert upstream_provider.requests == []


@pytest.mark.django_db(transaction=True)
def test_gate_off_keeps_local_only_authorization(
    user, workspace, tenant, upstream_provider, settings
):
    settings.UPSTREAM_ACCESS_FRESHNESS_ENFORCED = False
    make_proof_stale(user, tenant)
    upstream_provider.failure = 503

    assert resolve_workspace_access_ex(user, workspace.id).granted
    assert upstream_provider.requests == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_async_background_budget_rechecks_like_interactive(
    user, workspace, tenant, upstream_provider
):
    await amake_proof_stale(user, tenant)
    upstream_provider.domains = []

    result = await aresolve_workspace_access_ex(
        user, workspace.id, verification=VerificationBudget.BACKGROUND
    )

    assert result.denied_reason == UPSTREAM_ACCESS_LOST
    assert len(upstream_provider.requests) == 1


@pytest.mark.django_db(transaction=True)
def test_stale_read_takes_no_lease(user, tenant, workspace):
    membership = TenantMembership.objects.get(user=user, tenant=tenant)
    make_proof_stale(user, tenant)

    assert not proofs_are_fresh(user.id, membership.connection_id, {tenant.id})
    assert not VerificationControl.objects.filter(connection_id=membership.connection_id).exists()


@pytest.mark.django_db(transaction=True)
def test_unexpected_verification_error_is_a_retryable_denial(user, workspace, tenant):
    make_proof_stale(user, tenant)

    with patch(
        "apps.workspaces.services.access_freshness.verify_connection_access",
        AsyncMock(side_effect=RuntimeError("deadlock detected")),
    ):
        result = resolve_workspace_access_ex(user, workspace.id)

    assert result.denied_reason == VERIFICATION_UNAVAILABLE
    assert result.retryable
    assert TenantMembership.objects.filter(user=user, tenant=tenant).exists()


@pytest.mark.django_db(transaction=True)
def test_revocation_already_on_record_stays_a_coverage_loss(
    user, workspace, tenant, upstream_provider
):
    TenantMembership.objects.filter(user=user, tenant=tenant).update(archived_at=timezone.now())

    result = resolve_workspace_access_ex(user, workspace.id)

    assert result.denied_reason == TENANT_ACCESS_LOST
    assert upstream_provider.requests == []


@pytest.mark.django_db(transaction=True)
def test_no_provider_call_inside_a_callers_transaction(user, workspace, tenant, upstream_provider):
    make_proof_stale(user, tenant)
    upstream_provider.domains = [tenant.external_id]

    with transaction.atomic():
        result = resolve_workspace_access_ex(user, workspace.id)

    assert result.denied_reason == VERIFICATION_IN_PROGRESS
    assert result.retryable
    assert upstream_provider.requests == []


def test_every_freshness_denial_reason_has_a_public_message():
    assert set(access_module._FRESHNESS_MESSAGES) == set(FRESHNESS_DENIAL_REASONS)
