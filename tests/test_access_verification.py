import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from django.db import connection, transaction
from django.utils import timezone

from apps.common.error_codes import ErrorCode
from apps.users.adapters import encrypt_credential
from apps.users.models import (
    Tenant,
    TenantConnection,
    TenantMembership,
    UpstreamAccessProof,
    VerificationControl,
)
from apps.users.services.access_verification import (
    LEASE_DURATION,
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


def _ocs_connection(user, tenant, *, account_user=None, account_provider="ocs", scope="acme"):
    account = SocialAccount.objects.create(
        user=account_user or user,
        provider=account_provider,
        uid=f"42#{scope}" if scope else "42",
        extra_data={"team": scope},
    )
    SocialToken.objects.create(account=account, token="oauth-secret")
    conn = TenantConnection.objects.create(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.OAUTH,
        scope_key=scope,
        social_account=account,
    )
    membership = TenantMembership.objects.create(
        user=user,
        tenant=tenant,
        connection=conn,
        provider_metadata={"team_slug": scope},
    )
    return conn, membership


def _hold_user_lock(user_id, acquired, release):
    try:
        with transaction.atomic():
            user = TenantConnection._meta.get_field("user").remote_field.model
            user.objects.select_for_update().get(pk=user_id)
            acquired.set()
            assert release.wait(timeout=10)
    finally:
        connection.close()


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
def test_archived_membership_cannot_consume_fresh_proof(user, tenant, verification_connection):
    conn, membership = verification_connection
    now = timezone.now()
    claim = claim_verification(user.id, conn.id, {tenant.id}, now=now)
    publish_verification(claim, VerificationResult.complete({tenant.id}), now=now)
    trigger_tenant = Tenant.objects.create(
        provider=tenant.provider, external_id="trigger", canonical_name="Trigger"
    )
    TenantMembership.objects.create(user=user, tenant=trigger_tenant, connection=conn)
    trigger = claim_verification(
        user.id, conn.id, {trigger_tenant.id}, now=now + timedelta(minutes=1)
    )
    publish_verification(
        trigger,
        VerificationResult.complete({trigger_tenant.id}),
        now=now + timedelta(minutes=1),
    )
    membership.refresh_from_db()
    assert membership.archived_at is not None

    retry = claim_verification(user.id, conn.id, {tenant.id}, now=now + timedelta(minutes=2))

    assert retry.status == ClaimStatus.CLAIMED


@pytest.mark.django_db(transaction=True)
def test_freshness_clock_is_sampled_after_lock_wait(
    user, tenant, verification_connection, monkeypatch
):
    conn, _membership = verification_connection
    base = timezone.now()
    first = claim_verification(user.id, conn.id, {tenant.id}, now=base)
    publish_verification(first, VerificationResult.complete({tenant.id}), now=base)
    acquired = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(
        "apps.users.services.access_verification.timezone.now",
        lambda: base + (timedelta(minutes=5) if release.is_set() else timedelta(minutes=4)),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(_hold_user_lock, user.id, acquired, release)
        assert acquired.wait(timeout=10)
        waiter = executor.submit(claim_verification, user.id, conn.id, {tenant.id})
        time.sleep(0.1)
        assert not waiter.done()
        release.set()
        holder.result(timeout=10)
        result = waiter.result(timeout=10)

    assert result.status == ClaimStatus.CLAIMED


@pytest.mark.django_db(transaction=True)
def test_publication_clock_is_sampled_after_lock_wait(
    user, tenant, verification_connection, monkeypatch
):
    conn, _membership = verification_connection
    base = timezone.now()
    claim = claim_verification(user.id, conn.id, {tenant.id}, now=base)
    acquired = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(
        "apps.users.services.access_verification.timezone.now",
        lambda: (
            base + (LEASE_DURATION if release.is_set() else LEASE_DURATION - timedelta(seconds=1))
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(_hold_user_lock, user.id, acquired, release)
        assert acquired.wait(timeout=10)
        waiter = executor.submit(
            publish_verification, claim, VerificationResult.complete({tenant.id})
        )
        time.sleep(0.1)
        assert not waiter.done()
        release.set()
        holder.result(timeout=10)
        status = waiter.result(timeout=10)

    assert status == PublicationStatus.REJECTED
    assert not UpstreamAccessProof.objects.exists()


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
def test_ocs_complete_publication_preserves_other_team_memberships(user):
    acme = Tenant.objects.create(provider="ocs", external_id="acme-bot", canonical_name="Acme")
    globex = Tenant.objects.create(
        provider="ocs", external_id="globex-bot", canonical_name="Globex"
    )
    conn, _membership = _ocs_connection(user, acme, scope="acme")
    other_team = TenantMembership.objects.create(
        user=user,
        tenant=globex,
        connection=conn,
        provider_metadata={"team_slug": "globex"},
    )
    claim = claim_verification(user.id, conn.id, {acme.id})

    assert (
        publish_verification(claim, VerificationResult.complete({acme.id}))
        == PublicationStatus.PUBLISHED
    )

    other_team.refresh_from_db()
    assert other_team.archived_at is None


@pytest.mark.django_db
@pytest.mark.parametrize("provider_metadata", [{}, {"team_slug": ""}])
def test_ocs_complete_publication_recovers_legacy_team_tombstone(user, provider_metadata):
    current = Tenant.objects.create(
        provider="ocs", external_id=f"current-{bool(provider_metadata)}", canonical_name="Current"
    )
    legacy = Tenant.objects.create(
        provider="ocs", external_id=f"legacy-{bool(provider_metadata)}", canonical_name="Legacy"
    )
    conn, _membership = _ocs_connection(user, current, scope="acme")
    tombstone = TenantMembership.objects.create(
        user=user,
        tenant=legacy,
        connection=conn,
        provider_metadata=provider_metadata,
        archived_at=timezone.now(),
    )
    claim = claim_verification(user.id, conn.id, {legacy.id})

    assert (
        publish_verification(claim, VerificationResult.complete({legacy.id}))
        == PublicationStatus.PUBLISHED
    )

    tombstone.refresh_from_db()
    assert tombstone.archived_at is None
    assert tombstone.team_slug == "acme"
    assert UpstreamAccessProof.objects.filter(connection=conn, tenant=legacy).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("identity", ["blank_scope", "wrong_user", "wrong_provider"])
def test_ocs_oauth_claim_quarantines_untrusted_identity(user, other_user, identity):
    tenant = Tenant.objects.create(provider="ocs", external_id=identity, canonical_name=identity)
    kwargs = {}
    if identity == "blank_scope":
        kwargs["scope"] = ""
    elif identity == "wrong_user":
        kwargs["account_user"] = other_user
    else:
        kwargs["account_provider"] = "commcare"
    conn, _membership = _ocs_connection(user, tenant, **kwargs)

    claim = claim_verification(user.id, conn.id, {tenant.id})

    assert claim.status == ClaimStatus.DENIED


@pytest.mark.django_db
def test_valid_ocs_oauth_identity_can_claim(user):
    tenant = Tenant.objects.create(provider="ocs", external_id="valid", canonical_name="Valid")
    conn, _membership = _ocs_connection(user, tenant)

    assert claim_verification(user.id, conn.id, {tenant.id}).status == ClaimStatus.CLAIMED


@pytest.mark.django_db
def test_blank_scope_ocs_api_key_can_claim(user):
    tenant = Tenant.objects.create(
        provider="ocs", external_id="api-key-blank", canonical_name="API key"
    )
    conn = TenantConnection.objects.create(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("ocs-api-key"),
        scope_key="",
    )
    TenantMembership.objects.create(user=user, tenant=tenant, connection=conn)

    assert claim_verification(user.id, conn.id, {tenant.id}).status == ClaimStatus.CLAIMED


@pytest.mark.django_db
@pytest.mark.parametrize("encrypted", ["not-fernet", encrypt_credential("")])
def test_corrupt_or_empty_api_key_claim_is_safely_denied(
    user, tenant, verification_connection, encrypted
):
    conn, _membership = verification_connection
    conn.encrypted_credential = encrypted
    conn.save(update_fields=["encrypted_credential"])

    assert claim_verification(user.id, conn.id, {tenant.id}).status == ClaimStatus.DENIED


@pytest.mark.django_db
def test_corrupt_api_key_after_claim_rejects_publication(user, tenant, verification_connection):
    conn, _membership = verification_connection
    claim = claim_verification(user.id, conn.id, {tenant.id})
    conn.encrypted_credential = "not-fernet"
    conn.save(update_fields=["encrypted_credential"])

    assert (
        publish_verification(claim, VerificationResult.complete({tenant.id}))
        == PublicationStatus.REJECTED
    )
    assert not UpstreamAccessProof.objects.exists()


@pytest.mark.django_db
def test_empty_oauth_access_token_claim_is_safely_denied(user):
    tenant = Tenant.objects.create(
        provider="ocs", external_id="empty-token", canonical_name="Empty"
    )
    conn, _membership = _ocs_connection(user, tenant)
    SocialToken.objects.filter(account_id=conn.social_account_id).update(token="")

    assert claim_verification(user.id, conn.id, {tenant.id}).status == ClaimStatus.DENIED


@pytest.mark.django_db
def test_empty_oauth_access_token_after_claim_rejects_publication(user):
    tenant = Tenant.objects.create(
        provider="ocs", external_id="empty-token-publish", canonical_name="Empty"
    )
    conn, _membership = _ocs_connection(user, tenant)
    claim = claim_verification(user.id, conn.id, {tenant.id})
    SocialToken.objects.filter(account_id=conn.social_account_id).update(token="")

    assert (
        publish_verification(claim, VerificationResult.complete({tenant.id}))
        == PublicationStatus.REJECTED
    )
    assert not UpstreamAccessProof.objects.exists()


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
        claim, VerificationResult.tenant_denied(tenant.id, ErrorCode.AUTH_ACCESS_DENIED)
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
        claim, VerificationResult.credential_rejected(ErrorCode.AUTH_TOKEN_EXPIRED)
    )

    denied.refresh_from_db()
    untouched.refresh_from_db()
    conn.refresh_from_db()
    assert status == PublicationStatus.PUBLISHED
    assert denied.archived_at is not None
    assert untouched.archived_at is None
    assert conn.upstream_denial_code == ErrorCode.AUTH_TOKEN_EXPIRED


@pytest.mark.django_db
def test_invalid_authoritative_code_cannot_archive_membership(
    user, tenant, verification_connection
):
    conn, membership = verification_connection
    claim = claim_verification(user.id, conn.id, {tenant.id})

    status = publish_verification(claim, VerificationResult.credential_rejected("invented"))

    membership.refresh_from_db()
    conn.refresh_from_db()
    assert status == PublicationStatus.REJECTED
    assert membership.archived_at is None
    assert conn.upstream_denied_at is None
