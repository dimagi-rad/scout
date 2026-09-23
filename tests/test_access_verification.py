import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import uuid4

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
from apps.users.services import access_verification
from apps.users.services.access_verification import (
    LEASE_DURATION,
    ClaimStatus,
    PublicationStatus,
    VerificationDeadlineExceeded,
    claim_verification,
    proof_is_fresh,
    publish_verification,
    rebase_verification_claim,
    release_verification,
    snapshot_credential,
)
from apps.users.services.access_verification_types import (
    VerificationOutcome,
    VerificationResult,
)


@dataclass(frozen=True)
class _PersistedToken:
    """The credential fields rebase_verification_claim reads off a persisted snapshot.

    Mirrors PersistedTokenSnapshot, which lives in token_refresh and lands with the
    OAuth refresh-fencing work. Declared here so this module does not depend on that
    PR: rebase_verification_claim takes the snapshot untyped and reads only these
    fields, so the contract this exercises is entirely owned by access_verification.
    """

    token_id: int
    account_id: int
    app_id: int
    access_token: str
    refresh_token: str
    expires_at: object = None


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


def _hold_control_lock(connection_id, acquired, release):
    try:
        with transaction.atomic():
            VerificationControl.objects.select_for_update().get(connection_id=connection_id)
            acquired.set()
            assert release.wait(timeout=10)
    finally:
        connection.close()


@pytest.mark.django_db
def test_snapshot_credential_builds_observation_from_loaded_connection(
    user, verification_connection
):
    conn, _membership = verification_connection

    snapshot = snapshot_credential(conn)

    assert snapshot.observation.connection_id == conn.id
    assert snapshot.observation.user_id == user.id
    assert snapshot.credential == "secret-one"
    assert "secret-one" not in repr(snapshot)


@pytest.mark.django_db
def test_direct_empty_claim_is_denied_without_electing_lease(user, verification_connection):
    conn, _membership = verification_connection

    claim = claim_verification(user.id, conn.id, set())

    assert claim.status == ClaimStatus.DENIED
    assert not VerificationControl.objects.filter(connection=conn).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("tenant_provider", "expected"),
    [("ocs", ClaimStatus.DENIED), ("commcare-custom", ClaimStatus.CLAIMED)],
)
def test_claim_requires_same_canonical_provider(
    user, verification_connection, tenant_provider, expected
):
    conn, _membership = verification_connection
    mismatched = Tenant.objects.create(
        provider=tenant_provider,
        external_id=f"provider-{tenant_provider}",
        canonical_name="Provider scope",
    )
    TenantMembership.objects.create(user=user, tenant=mismatched, connection=conn)

    claim = claim_verification(user.id, conn.id, {mismatched.id})

    assert claim.status == expected
    assert VerificationControl.objects.filter(connection=conn).exists() is (
        expected == ClaimStatus.CLAIMED
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({}, ClaimStatus.CLAIMED),
        ({"team_slug": ""}, ClaimStatus.CLAIMED),
        ({"team_slug": "acme"}, ClaimStatus.CLAIMED),
        ({"team_slug": "other-team"}, ClaimStatus.DENIED),
    ],
)
def test_ocs_claim_allows_legacy_team_history_but_denies_explicit_other_team(
    user, metadata, expected
):
    tenant = Tenant.objects.create(
        provider="ocs", external_id=f"scope-{metadata!s}", canonical_name="Scoped"
    )
    conn, membership = _ocs_connection(user, tenant, scope="acme")
    membership.provider_metadata = metadata
    membership.save(update_fields=["provider_metadata"])

    claim = claim_verification(user.id, conn.id, {tenant.id})

    assert claim.status == expected
    assert VerificationControl.objects.filter(connection=conn).exists() is (
        expected == ClaimStatus.CLAIMED
    )


@pytest.mark.django_db
def test_oauth_claim_without_social_account_is_denied(user, tenant):
    conn = TenantConnection.objects.create(
        user=user,
        provider="commcare",
        credential_type=TenantConnection.OAUTH,
        social_account=None,
    )
    TenantMembership.objects.create(user=user, tenant=tenant, connection=conn)

    claim = claim_verification(user.id, conn.id, {tenant.id})

    assert claim.status == ClaimStatus.DENIED
    assert not VerificationControl.objects.filter(connection=conn).exists()


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


@pytest.mark.django_db(transaction=True)
def test_claim_user_lock_wait_stops_at_deadline(user, tenant, verification_connection):
    conn, _membership = verification_connection
    acquired = threading.Event()
    release = threading.Event()
    locker = threading.Thread(target=_hold_user_lock, args=(user.id, acquired, release))
    locker.start()
    assert acquired.wait(timeout=2)
    timer = threading.Timer(0.4, release.set)
    timer.start()

    started = time.monotonic()
    claim = claim_verification(
        user.id,
        conn.id,
        {tenant.id},
        deadline=started + 0.1,
    )
    elapsed = time.monotonic() - started
    locker.join(timeout=2)
    timer.cancel()

    assert claim.status == ClaimStatus.DEADLINE
    assert elapsed < 0.3
    assert not VerificationControl.objects.filter(connection=conn).exists()


@pytest.mark.django_db(transaction=True)
def test_rebase_user_lock_wait_stops_at_deadline(user):
    tenant = Tenant.objects.create(
        provider="ocs", external_id="deadline", canonical_name="Deadline"
    )
    conn, _membership = _ocs_connection(user, tenant)
    claim = claim_verification(user.id, conn.id, {tenant.id})
    token = SocialToken.objects.get(account_id=conn.social_account_id)
    persisted = _PersistedToken(
        token_id=token.id,
        account_id=token.account_id,
        app_id=token.app_id,
        access_token=token.token,
        refresh_token=token.token_secret,
        expires_at=token.expires_at,
    )
    acquired = threading.Event()
    release = threading.Event()
    locker = threading.Thread(target=_hold_user_lock, args=(user.id, acquired, release))
    locker.start()
    assert acquired.wait(timeout=2)
    timer = threading.Timer(0.4, release.set)
    timer.start()

    started = time.monotonic()
    with pytest.raises(VerificationDeadlineExceeded):
        rebase_verification_claim(
            claim,
            persisted,
            deadline=started + 0.1,
        )
    elapsed = time.monotonic() - started
    locker.join(timeout=2)
    timer.cancel()
    release_verification(claim)

    assert elapsed < 0.3
    assert VerificationControl.objects.get(connection=conn).lease_token is None


@pytest.mark.django_db(transaction=True)
def test_release_is_bounded_when_control_row_is_locked(user, tenant, verification_connection):
    conn, _membership = verification_connection
    claim = claim_verification(user.id, conn.id, {tenant.id})
    acquired = threading.Event()
    release = threading.Event()
    locker = threading.Thread(target=_hold_control_lock, args=(conn.id, acquired, release))
    locker.start()
    # Release in a finally: a failed assertion here would otherwise leave the locker
    # holding its row lock for its full 10s, stalling this transactional test's
    # teardown truncate and cascading into neighbouring tests.
    try:
        assert acquired.wait(timeout=2)

        started = time.monotonic()
        released = release_verification(claim)
        elapsed = time.monotonic() - started

        assert released is False
        assert elapsed < 0.2
    finally:
        release.set()
        locker.join(timeout=2)
    assert VerificationControl.objects.get(connection=conn).lease_token == claim.lease_token
    assert release_verification(claim) is True


@pytest.mark.django_db
def test_release_never_clears_replacement_lease(user, tenant, verification_connection):
    conn, _membership = verification_connection
    claim = claim_verification(user.id, conn.id, {tenant.id})
    successor = uuid4()
    VerificationControl.objects.filter(connection=conn).update(lease_token=successor)

    assert release_verification(claim) is False
    assert VerificationControl.objects.get(connection=conn).lease_token == successor


@pytest.mark.django_db(transaction=True)
def test_claim_recomputes_remaining_deadline_before_control_lock(
    user, tenant, verification_connection, monkeypatch
):
    conn, _membership = verification_connection
    VerificationControl.objects.create(connection=conn)
    # A stale proof keeps the claim past the freshness check and gives us a hook that
    # runs after the proofs query's recompute but before the control lock, so the
    # budget this test spends lands in exactly the window the recompute must cover.
    UpstreamAccessProof.objects.create(
        connection=conn,
        tenant=tenant,
        verified_at=timezone.now() - timedelta(minutes=6),
        credential_fingerprint=snapshot_credential(conn).observation.credential_fingerprint,
    )
    acquired = threading.Event()
    release = threading.Event()
    locker = threading.Thread(target=_hold_control_lock, args=(conn.id, acquired, release))
    locker.start()
    timer = threading.Timer(0.5, release.set)
    timer.start()
    try:
        assert acquired.wait(timeout=2)

        events = []
        original_configure = access_verification._configure_transaction_deadline
        original_fresh = access_verification.proof_is_fresh

        def recording_configure(*args, **kwargs):
            events.append("configure")
            return original_configure(*args, **kwargs)

        def delayed_fresh(*args, **kwargs):
            time.sleep(0.08)
            events.append("budget-spent")
            return original_fresh(*args, **kwargs)

        monkeypatch.setattr(
            access_verification, "_configure_transaction_deadline", recording_configure
        )
        monkeypatch.setattr(access_verification, "proof_is_fresh", delayed_fresh)
        started = time.monotonic()
        claim = claim_verification(
            user.id,
            conn.id,
            {tenant.id},
            deadline=started + 0.15,
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
        locker.join(timeout=2)
        timer.cancel()

    assert claim.status == ClaimStatus.DEADLINE
    # The guard: the deadline must be recomputed after the budget was spent and before
    # the control lock. Without that recompute the lock would inherit the stale, larger
    # timeout computed back at the proofs query. Asserting on ordering rather than wall
    # clock keeps this deterministic on a loaded runner.
    assert "budget-spent" in events
    assert "configure" in events[events.index("budget-spent") :]
    # Loose bound: it only has to prove we did not block for the locker's full hold.
    assert elapsed < 0.45
    assert VerificationControl.objects.get(connection=conn).lease_token is None


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
def test_direct_archival_cannot_consume_still_fresh_proof(user, tenant, verification_connection):
    conn, membership = verification_connection
    now = timezone.now()
    claim = claim_verification(user.id, conn.id, {tenant.id}, now=now)
    assert (
        publish_verification(claim, VerificationResult.complete({tenant.id}), now=now)
        == PublicationStatus.PUBLISHED
    )
    retry_at = now + timedelta(minutes=1)
    assert (
        claim_verification(user.id, conn.id, {tenant.id}, now=retry_at).status == ClaimStatus.FRESH
    )

    # Direct archival changes membership liveness without invalidating the proof.
    membership.archived_at = retry_at
    membership.save(update_fields=["archived_at"])
    proof = UpstreamAccessProof.objects.get(connection=conn, tenant=tenant)
    assert proof.verified_at == now
    assert proof_is_fresh(proof, claim.observation, now=retry_at)

    retry = claim_verification(user.id, conn.id, {tenant.id}, now=retry_at)

    assert retry.status == ClaimStatus.CLAIMED


@pytest.mark.django_db
@pytest.mark.parametrize("legacy_restores", [False, True])
def test_archived_membership_cannot_consume_fresh_proof(
    user, tenant, verification_connection, legacy_restores
):
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
    if legacy_restores:
        # A pre-omission legacy discovery can finish later with the same token.
        membership.archived_at = None
        membership.save(update_fields=["archived_at"])

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

    control = VerificationControl.objects.get(connection=conn)
    assert control.lease_token is None
    assert control.lease_expires_at is None
    conn.encrypted_credential = encrypt_credential("repaired-key")
    conn.save(update_fields=["encrypted_credential"])
    assert claim_verification(user.id, conn.id, {tenant.id}).status == ClaimStatus.CLAIMED


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

    control = VerificationControl.objects.get(connection=conn)
    assert control.lease_token is None
    assert control.lease_expires_at is None
    SocialToken.objects.filter(account_id=conn.social_account_id).update(token="repaired-token")
    assert claim_verification(user.id, conn.id, {tenant.id}).status == ClaimStatus.CLAIMED


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


@pytest.mark.django_db
def test_invalid_snapshot_publication_preserves_successor_lease(
    user, tenant, verification_connection
):
    conn, _membership = verification_connection
    claim = claim_verification(user.id, conn.id, {tenant.id})
    successor = uuid4()
    expires = timezone.now() + LEASE_DURATION
    VerificationControl.objects.filter(connection=conn).update(
        lease_token=successor, lease_expires_at=expires
    )
    conn.encrypted_credential = "not-fernet"
    conn.save(update_fields=["encrypted_credential"])

    assert (
        publish_verification(claim, VerificationResult.complete({tenant.id}))
        == PublicationStatus.REJECTED
    )
    control = VerificationControl.objects.get(connection=conn)
    assert control.lease_token == successor
    assert control.lease_expires_at == expires
    assert not UpstreamAccessProof.objects.exists()


@pytest.mark.django_db
def test_waiter_cannot_release_winners_lease(user, tenant, verification_connection):
    conn, _membership = verification_connection
    winner = claim_verification(user.id, conn.id, {tenant.id})
    waiter = claim_verification(user.id, conn.id, {tenant.id})
    assert winner.status == ClaimStatus.CLAIMED
    assert waiter.status == ClaimStatus.IN_PROGRESS
    assert waiter.lease_token == winner.lease_token

    assert release_verification(waiter) is False

    control = VerificationControl.objects.get(connection=conn)
    assert control.lease_token == winner.lease_token
    assert (
        publish_verification(winner, VerificationResult.complete({tenant.id}))
        == PublicationStatus.PUBLISHED
    )


@pytest.mark.django_db
@pytest.mark.parametrize("returned", [False, True])
def test_canonical_provider_history_can_be_published_or_omitted(
    user, verification_connection, returned
):
    conn, _membership = verification_connection
    alias = Tenant.objects.create(
        provider="commcare-custom", external_id="alias", canonical_name="Alias"
    )
    foreign = Tenant.objects.create(
        provider="commcare_connect", external_id="foreign", canonical_name="Foreign"
    )
    membership = TenantMembership.objects.create(user=user, tenant=alias, connection=conn)
    foreign_membership = TenantMembership.objects.create(user=user, tenant=foreign, connection=conn)
    proof = UpstreamAccessProof.objects.create(
        connection=conn,
        tenant=alias,
        verified_at=timezone.now() - timedelta(minutes=6),
        credential_fingerprint=snapshot_credential(conn).observation.credential_fingerprint,
    )
    claim = claim_verification(user.id, conn.id, {alias.id})
    assert claim.status == ClaimStatus.CLAIMED
    result = VerificationResult.complete({alias.id, foreign.id} if returned else set())

    assert publish_verification(claim, result) == PublicationStatus.PUBLISHED

    membership.refresh_from_db()
    foreign_membership.refresh_from_db()
    proof.refresh_from_db()
    assert foreign_membership.archived_at is None
    assert not UpstreamAccessProof.objects.filter(connection=conn, tenant=foreign).exists()
    if returned:
        assert membership.archived_at is None
        assert proof_is_fresh(proof, claim.observation)
        assert claim_verification(user.id, conn.id, {alias.id}).status == ClaimStatus.FRESH
    else:
        assert membership.archived_at is not None
        assert proof.verified_at is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("operation", ["claim", "publish", "release", "rebase", "denied_claim"])
def test_verification_restores_timeouts_inside_outer_transaction(user, operation):
    tenant = Tenant.objects.create(
        provider="ocs", external_id="timeouts", canonical_name="Timeouts"
    )
    conn, _membership = _ocs_connection(user, tenant)
    claim = claim_verification(user.id, conn.id, {tenant.id}) if operation != "claim" else None
    token = SocialToken.objects.get(account_id=conn.social_account_id)
    persisted = _PersistedToken(
        token_id=token.id,
        account_id=token.account_id,
        app_id=token.app_id,
        access_token=token.token,
        refresh_token=token.token_secret,
        expires_at=token.expires_at,
    )
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('lock_timeout', '3s', true), set_config('statement_timeout', '4s', true)"
            )
        deadline = time.monotonic() + 0.5
        if operation == "claim":
            assert (
                claim_verification(user.id, conn.id, {tenant.id}, deadline=deadline).status
                == ClaimStatus.CLAIMED
            )
        elif operation == "publish":
            assert (
                publish_verification(
                    claim, VerificationResult.complete({tenant.id}), deadline=deadline
                )
                == PublicationStatus.PUBLISHED
            )
        elif operation == "release":
            assert release_verification(claim)
        elif operation == "rebase":
            assert rebase_verification_claim(claim, persisted, deadline=deadline) is not None
        else:
            assert (
                claim_verification(user.id, uuid4(), {tenant.id}, deadline=deadline).status
                == ClaimStatus.DENIED
            )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
            )
            assert cursor.fetchone() == ("3s", "4s")


def _publish_and_count_recomputes(user, tenant_count, monkeypatch, label):
    conn = TenantConnection.objects.create(
        user=user,
        provider="commcare",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential(f"secret-{label}"),
    )
    tenant_ids = set()
    for index in range(tenant_count):
        extra = Tenant.objects.create(
            provider="commcare",
            external_id=f"{label}-{index}",
            canonical_name=f"{label} {index}",
        )
        TenantMembership.objects.create(user=user, tenant=extra, connection=conn)
        tenant_ids.add(extra.id)
    claim = claim_verification(user.id, conn.id, tenant_ids)
    assert claim.status == ClaimStatus.CLAIMED

    original = access_verification._configure_transaction_deadline
    recomputes = []

    def counting_configure(deadline, clock, cancelled=None):
        recomputes.append(1)
        return original(deadline, clock, cancelled)

    monkeypatch.setattr(access_verification, "_configure_transaction_deadline", counting_configure)
    try:
        status = publish_verification(
            claim, VerificationResult.complete(tenant_ids), deadline=time.monotonic() + 30
        )
    finally:
        monkeypatch.setattr(access_verification, "_configure_transaction_deadline", original)
    assert status == PublicationStatus.PUBLISHED
    assert UpstreamAccessProof.objects.filter(connection=conn).count() == tenant_count
    return len(recomputes)


@pytest.mark.django_db
def test_publish_recomputes_deadline_per_tenant_write(user, monkeypatch):
    one = _publish_and_count_recomputes(user, 1, monkeypatch, "single")
    many = _publish_and_count_recomputes(user, 7, monkeypatch, "bulk")

    # lock_timeout/statement_timeout are per statement, so a budget set once cannot
    # bound a loop over N tenants: each write may wait the full remaining budget and
    # total wall time grows with N while the wrapper still advertises a bounded wait.
    # Comparing two sizes isolates per-tenant scaling from the constant overhead, so
    # this fails if the per-write recomputes are dropped.
    assert many - one >= 6


@pytest.mark.django_db(transaction=True)
def test_attempt_receipt_is_scoped_to_the_tenants_it_covered(user, tenant):
    """A waiter must not reuse a receipt from an attempt that skipped its tenant.

    Replaces the earlier test that pinned this as a documented limitation: the receipt
    now records its tenant scope, so the mismatch is refused rather than described.
    """
    conn = TenantConnection.objects.create(
        user=user,
        provider=tenant.provider,
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("secret-scope"),
    )
    TenantMembership.objects.create(user=user, tenant=tenant, connection=conn)
    other = Tenant.objects.create(
        provider=tenant.provider, external_id="receipt-other", canonical_name="Other"
    )
    other_membership = TenantMembership.objects.create(user=user, tenant=other, connection=conn)

    winner = claim_verification(user.id, conn.id, {tenant.id})
    assert winner.status == ClaimStatus.CLAIMED
    assert publish_verification(winner, VerificationResult.complete({tenant.id})) == (
        PublicationStatus.PUBLISHED
    )

    other_membership.refresh_from_db()
    assert other_membership.archived_at is not None

    control = VerificationControl.objects.get(connection=conn)
    receipt = access_verification._completed_attempt(control)
    assert receipt is not None
    assert receipt.outcome == VerificationOutcome.COMPLETE
    assert receipt.tenant_ids == frozenset({str(tenant.id)})

    # The tenant the attempt actually confirmed still matches.
    assert access_verification.attempt_receipt_matches(
        receipt, control.last_attempt_lease_token, winner.observation, {tenant.id}
    )
    # The tenant it archived as omitted does not, nor does a set spanning both.
    assert not access_verification.attempt_receipt_matches(
        receipt, control.last_attempt_lease_token, winner.observation, {other.id}
    )
    assert not access_verification.attempt_receipt_matches(
        receipt, control.last_attempt_lease_token, winner.observation, {tenant.id, other.id}
    )
    # An empty request proves nothing and must not match.
    assert not access_verification.attempt_receipt_matches(
        receipt, control.last_attempt_lease_token, winner.observation, set()
    )


@pytest.mark.django_db
def test_credential_level_receipt_covers_every_requested_tenant(user, verification_connection):
    conn, membership = verification_connection
    second = Tenant.objects.create(
        provider=conn.provider, external_id="cred-level", canonical_name="Second"
    )
    TenantMembership.objects.create(user=user, tenant=second, connection=conn)
    requested = {membership.tenant_id, second.id}
    claim = claim_verification(user.id, conn.id, requested)
    assert claim.status == ClaimStatus.CLAIMED

    assert (
        publish_verification(
            claim,
            VerificationResult.credential_rejected(ErrorCode.AUTH_TOKEN_EXPIRED),
        )
        == PublicationStatus.PUBLISHED
    )

    control = VerificationControl.objects.get(connection=conn)
    receipt = access_verification._completed_attempt(control)
    assert receipt.tenant_ids == frozenset(str(tenant_id) for tenant_id in requested)
    assert access_verification.attempt_receipt_matches(
        receipt, control.last_attempt_lease_token, claim.observation, requested
    )


@pytest.mark.django_db
def test_receipt_without_recorded_scope_matches_nothing(user, verification_connection):
    """Rows written before the scope existed carry [] and must fail closed."""
    conn, membership = verification_connection
    claim = claim_verification(user.id, conn.id, {membership.tenant_id})
    assert claim.status == ClaimStatus.CLAIMED
    publish_verification(claim, VerificationResult.complete({membership.tenant_id}))
    VerificationControl.objects.filter(connection=conn).update(last_attempt_tenant_ids=[])

    control = VerificationControl.objects.get(connection=conn)
    receipt = access_verification._completed_attempt(control)

    assert receipt.tenant_ids == frozenset()
    assert not access_verification.attempt_receipt_matches(
        receipt, control.last_attempt_lease_token, claim.observation, {membership.tenant_id}
    )


@pytest.mark.django_db
def test_complete_receipt_excludes_a_tenant_it_omitted(user, verification_connection):
    """A COMPLETE attempt that archived a requested tenant must not vouch for it.

    The claim asks about two tenants and upstream returns only one, so the other is
    archived as omitted. Recording the requested set rather than the accepted set here
    would let a consumer read this COMPLETE as success for the revoked tenant.
    """
    conn, membership = verification_connection
    omitted = Tenant.objects.create(
        provider=conn.provider, external_id="omitted-one", canonical_name="Omitted"
    )
    omitted_membership = TenantMembership.objects.create(user=user, tenant=omitted, connection=conn)
    requested = {membership.tenant_id, omitted.id}
    claim = claim_verification(user.id, conn.id, requested)
    assert claim.status == ClaimStatus.CLAIMED

    assert (
        publish_verification(claim, VerificationResult.complete({membership.tenant_id}))
        == PublicationStatus.PUBLISHED
    )

    omitted_membership.refresh_from_db()
    assert omitted_membership.archived_at is not None

    control = VerificationControl.objects.get(connection=conn)
    receipt = access_verification._completed_attempt(control)
    assert receipt.tenant_ids == frozenset({str(membership.tenant_id)})
    assert access_verification.attempt_receipt_matches(
        receipt, control.last_attempt_lease_token, claim.observation, {membership.tenant_id}
    )
    assert not access_verification.attempt_receipt_matches(
        receipt, control.last_attempt_lease_token, claim.observation, {omitted.id}
    )
    assert not access_verification.attempt_receipt_matches(
        receipt, control.last_attempt_lease_token, claim.observation, requested
    )


@pytest.mark.django_db
def test_tenant_denied_receipt_covers_only_the_denied_tenant(user, verification_connection):
    """A denial is about one tenant and must not be reused for the others.

    Recording the requested set here would let a consumer treat a sibling tenant as
    denied on the strength of a denial that never named it.
    """
    conn, membership = verification_connection
    sibling = Tenant.objects.create(
        provider=conn.provider, external_id="denied-sibling", canonical_name="Sibling"
    )
    TenantMembership.objects.create(user=user, tenant=sibling, connection=conn)
    requested = {membership.tenant_id, sibling.id}
    claim = claim_verification(user.id, conn.id, requested)
    assert claim.status == ClaimStatus.CLAIMED

    assert (
        publish_verification(
            claim,
            VerificationResult.tenant_denied(membership.tenant_id, ErrorCode.AUTH_ACCESS_DENIED),
        )
        == PublicationStatus.PUBLISHED
    )

    control = VerificationControl.objects.get(connection=conn)
    receipt = access_verification._completed_attempt(control)
    assert receipt.outcome == VerificationOutcome.TENANT_DENIED
    assert receipt.tenant_ids == frozenset({str(membership.tenant_id)})
    assert access_verification.attempt_receipt_matches(
        receipt, control.last_attempt_lease_token, claim.observation, {membership.tenant_id}
    )
    assert not access_verification.attempt_receipt_matches(
        receipt, control.last_attempt_lease_token, claim.observation, {sibling.id}
    )


def _receipt(outcome, observation, tenant_ids, lease_token):
    return access_verification.VerificationAttemptReceipt(
        lease_token=lease_token,
        outcome=outcome,
        error_code="",
        observation_hash=access_verification._observation_hash(observation),
        tenant_ids=frozenset(str(tenant_id) for tenant_id in tenant_ids),
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("outcome", "reusable_for_uncovered"),
    [
        (VerificationOutcome.COMPLETE, False),
        (VerificationOutcome.TENANT_DENIED, False),
        (VerificationOutcome.CREDENTIAL_REJECTED, True),
        (VerificationOutcome.UNAVAILABLE, True),
        (VerificationOutcome.INDETERMINATE, True),
    ],
)
def test_receipt_scope_gate_applies_only_to_tenant_scoped_outcomes(
    verification_connection, outcome, reusable_for_uncovered
):
    """The matcher owns the whole scope policy, in both directions.

    A tenant-scoped verdict must not vouch for a tenant it never covered, while a
    connection-level verdict must, or a waiter asking about other tenants on the same
    failed connection would force a second upstream call.
    """
    conn, membership = verification_connection
    observation = snapshot_credential(conn).observation
    lease = uuid4()
    uncovered = uuid4()
    receipt = _receipt(outcome, observation, {membership.tenant_id}, lease)

    matches = access_verification.attempt_receipt_matches
    assert matches(receipt, lease, observation, {membership.tenant_id})
    assert matches(receipt, lease, observation, {uncovered}) is reusable_for_uncovered
    assert (
        matches(receipt, lease, observation, {membership.tenant_id, uncovered})
        is reusable_for_uncovered
    )
    # The scope exemption must not weaken the lease and observation gates.
    assert not matches(receipt, uuid4(), observation, {uncovered})
    assert not matches(
        receipt, lease, replace(observation, credential_fingerprint="rotated"), {uncovered}
    )
    assert not matches(receipt, lease, observation, set())


@pytest.mark.django_db
@pytest.mark.parametrize("outcome", list(VerificationOutcome))
def test_unscoped_legacy_receipt_matches_nothing_for_any_outcome(verification_connection, outcome):
    conn, membership = verification_connection
    observation = snapshot_credential(conn).observation
    lease = uuid4()
    receipt = _receipt(outcome, observation, (), lease)

    assert not access_verification.attempt_receipt_matches(
        receipt, lease, observation, {membership.tenant_id}
    )
