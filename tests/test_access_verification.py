import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from apps.users.adapters import encrypt_credential
from apps.users.models import (
    Tenant,
    TenantConnection,
    TenantMembership,
    UpstreamAccessProof,
    VerificationControl,
)
from apps.users.services.access_verification import (
    ClaimStatus,
    PublicationStatus,
    claim_verification,
    proof_is_fresh,
    publish_verification,
)
from apps.users.services.access_verification_types import VerificationResult


@pytest.fixture
def verification_connection(user, tenant):
    conn = TenantConnection.objects.create(
        user=user,
        provider=tenant.provider,
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("secret-one"),
    )
    membership = TenantMembership.objects.create(user=user, tenant=tenant, connection=conn)
    return conn, membership


@pytest.mark.django_db(transaction=True)
def test_disjoint_requests_elect_one_connection_winner(user, tenant, verification_connection):
    conn, _membership = verification_connection
    other = Tenant.objects.create(
        provider=tenant.provider, external_id="other-domain", canonical_name="Other"
    )
    TenantMembership.objects.create(user=user, tenant=other, connection=conn)
    barrier = threading.Barrier(2)

    def claim(tenant_id):
        try:
            barrier.wait(timeout=10)
            return claim_verification(user.id, conn.id, {tenant_id}).status
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = {
            future.result(timeout=10)
            for future in (
                executor.submit(claim, tenant.id),
                executor.submit(claim, other.id),
            )
        }

    assert statuses == {ClaimStatus.CLAIMED, ClaimStatus.IN_PROGRESS}
    assert VerificationControl.objects.get(connection=conn).lease_token is not None


@pytest.mark.django_db
def test_proof_freshness_requires_exact_identity_and_five_minute_boundary(
    user, tenant, verification_connection
):
    conn, _membership = verification_connection
    now = timezone.now()
    claim = claim_verification(user.id, conn.id, {tenant.id}, now=now)
    assert claim.status == ClaimStatus.CLAIMED
    assert "secret-one" not in repr(claim)
    assert (
        publish_verification(claim, VerificationResult.complete({tenant.id}), now=now)
        == PublicationStatus.PUBLISHED
    )
    proof = UpstreamAccessProof.objects.get(connection=conn, tenant=tenant)

    assert proof_is_fresh(proof, claim.observation, now=now + timedelta(minutes=4, seconds=59))
    assert not proof_is_fresh(proof, claim.observation, now=now + timedelta(minutes=5))
    changed = claim.observation.with_scope("changed")
    assert not proof_is_fresh(proof, changed, now=now + timedelta(minutes=1))


@pytest.mark.django_db
def test_expired_owner_cannot_publish_without_successor(user, tenant, verification_connection):
    conn, _membership = verification_connection
    now = timezone.now()
    claim = claim_verification(user.id, conn.id, {tenant.id}, now=now)

    status = publish_verification(
        claim,
        VerificationResult.complete({tenant.id}),
        now=claim.lease_expires_at,
    )

    assert status == PublicationStatus.REJECTED
    assert not UpstreamAccessProof.objects.exists()


@pytest.mark.django_db
def test_replaced_lease_rejects_old_winner(user, tenant, verification_connection):
    conn, _membership = verification_connection
    now = timezone.now()
    old = claim_verification(user.id, conn.id, {tenant.id}, now=now)
    new = claim_verification(
        user.id, conn.id, {tenant.id}, now=old.lease_expires_at + timedelta(microseconds=1)
    )
    assert new.status == ClaimStatus.CLAIMED

    assert (
        publish_verification(
            old, VerificationResult.complete({tenant.id}), now=now + timedelta(seconds=1)
        )
        == PublicationStatus.REJECTED
    )


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["credential", "denial"])
def test_rotation_or_denial_fence_rejects_publication(
    user, tenant, verification_connection, change
):
    conn, _membership = verification_connection
    claim = claim_verification(user.id, conn.id, {tenant.id})
    if change == "credential":
        conn.encrypted_credential = encrypt_credential("secret-two")
        conn.save(update_fields=["encrypted_credential"])
    else:
        conn.upstream_denied_at = timezone.now()
        conn.save(update_fields=["upstream_denied_at"])

    assert (
        publish_verification(claim, VerificationResult.complete({tenant.id}))
        == PublicationStatus.REJECTED
    )
    assert not UpstreamAccessProof.objects.exists()


@pytest.mark.django_db
def test_complete_publication_restores_only_observed_connection_history(
    user, tenant, verification_connection
):
    conn, own = verification_connection
    conn.upstream_denial_code = "credential_expired"
    conn.upstream_denied_at = timezone.now()
    conn.save(update_fields=["upstream_denial_code", "upstream_denied_at"])
    own.archived_at = timezone.now()
    own.save(update_fields=["archived_at"])
    other_conn = TenantConnection.objects.create(
        user=user,
        provider=tenant.provider,
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("other-secret"),
    )
    other_tenant = Tenant.objects.create(
        provider=tenant.provider, external_id="other-owned", canonical_name="Other owned"
    )
    other_membership = TenantMembership.objects.create(
        user=user,
        tenant=other_tenant,
        connection=other_conn,
        archived_at=timezone.now(),
    )
    omitted_tenant = Tenant.objects.create(
        provider=tenant.provider, external_id="omitted", canonical_name="Omitted"
    )
    omitted = TenantMembership.objects.create(user=user, tenant=omitted_tenant, connection=conn)
    claim = claim_verification(user.id, conn.id, {tenant.id})

    assert (
        publish_verification(claim, VerificationResult.complete({tenant.id, other_tenant.id}))
        == PublicationStatus.PUBLISHED
    )

    own.refresh_from_db()
    other_membership.refresh_from_db()
    assert own.archived_at is None
    assert other_membership.archived_at is not None
    omitted.refresh_from_db()
    assert omitted.archived_at is not None
    assert UpstreamAccessProof.objects.filter(connection=conn, tenant=tenant).exists()
    assert not UpstreamAccessProof.objects.filter(tenant=other_tenant).exists()
    conn.refresh_from_db()
    assert conn.upstream_denial_code == ""


@pytest.mark.django_db
@pytest.mark.parametrize(
    "result", [VerificationResult.unavailable("timeout"), VerificationResult.indeterminate("shape")]
)
def test_non_authoritative_outcomes_preserve_proof_and_membership(
    user, tenant, verification_connection, result
):
    conn, membership = verification_connection
    first = claim_verification(user.id, conn.id, {tenant.id})
    publish_verification(first, VerificationResult.complete({tenant.id}))
    proof = UpstreamAccessProof.objects.get(connection=conn, tenant=tenant)
    verified_at = proof.verified_at
    retry = claim_verification(
        user.id, conn.id, {tenant.id}, now=verified_at + timedelta(minutes=6)
    )

    assert publish_verification(retry, result) == PublicationStatus.PUBLISHED
    proof.refresh_from_db()
    membership.refresh_from_db()
    assert proof.verified_at == verified_at
    assert membership.archived_at is None
    assert proof.last_attempt_result == result.outcome.value
    assert proof.last_error_code == result.error_code


@pytest.mark.django_db
def test_first_unavailable_attempt_records_no_positive_proof(user, tenant, verification_connection):
    conn, membership = verification_connection
    claim = claim_verification(user.id, conn.id, {tenant.id})

    assert (
        publish_verification(claim, VerificationResult.unavailable("timeout"))
        == PublicationStatus.PUBLISHED
    )

    proof = UpstreamAccessProof.objects.get(connection=conn, tenant=tenant)
    membership.refresh_from_db()
    assert proof.verified_at is None
    assert proof.last_attempt_result == "unavailable"
    assert membership.archived_at is None


@pytest.mark.django_db
def test_authoritative_tenant_denial_archives_only_that_tenant(
    user, tenant, verification_connection
):
    conn, denied = verification_connection
    sibling_tenant = Tenant.objects.create(
        provider=tenant.provider, external_id="sibling", canonical_name="Sibling"
    )
    sibling = TenantMembership.objects.create(user=user, tenant=sibling_tenant, connection=conn)
    claim = claim_verification(user.id, conn.id, {tenant.id, sibling_tenant.id})

    status = publish_verification(
        claim, VerificationResult.tenant_denied(tenant.id, "upstream_access_lost")
    )

    denied.refresh_from_db()
    sibling.refresh_from_db()
    conn.refresh_from_db()
    assert status == PublicationStatus.PUBLISHED
    assert denied.archived_at is not None
    assert sibling.archived_at is None
    assert conn.upstream_denied_at is not None


@pytest.mark.django_db
def test_authoritative_credential_rejection_archives_only_observed_connection(
    user, tenant, verification_connection
):
    conn, denied = verification_connection
    other_tenant = Tenant.objects.create(
        provider=tenant.provider, external_id="other-credential", canonical_name="Other credential"
    )
    other_conn = TenantConnection.objects.create(
        user=user,
        provider=tenant.provider,
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("other-secret"),
    )
    untouched = TenantMembership.objects.create(
        user=user, tenant=other_tenant, connection=other_conn
    )
    claim = claim_verification(user.id, conn.id, {tenant.id})

    status = publish_verification(
        claim, VerificationResult.credential_rejected("credential_expired")
    )

    denied.refresh_from_db()
    untouched.refresh_from_db()
    conn.refresh_from_db()
    assert status == PublicationStatus.PUBLISHED
    assert denied.archived_at is not None
    assert untouched.archived_at is None
    assert conn.upstream_denial_code == "credential_expired"
