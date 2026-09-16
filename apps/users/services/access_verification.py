from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from allauth.socialaccount.models import SocialToken
from asgiref.sync import sync_to_async
from cryptography.fernet import InvalidToken
from django.db import OperationalError, transaction
from django.db import connection as django_connection
from django.db.models import Q
from django.utils import timezone

from apps.common.error_codes import ErrorCode
from apps.users.adapters import decrypt_credential
from apps.users.models import (
    TenantConnection,
    TenantMembership,
    UpstreamAccessProof,
    User,
    VerificationControl,
)
from apps.users.services.access_verification_types import (
    CredentialObservation,
    CredentialRequestSnapshot,
    VerificationOutcome,
    VerificationResult,
)
from apps.users.services.oauth_scope import account_scope, canonical_provider, provider_accounts
from apps.users.services.token_refresh import credential_fingerprint
from apps.users.services.upstream_denial import record_validated_upstream_denial

PROOF_MAX_AGE = timedelta(minutes=5)
LEASE_DURATION = timedelta(seconds=30)
CLEANUP_TIMEOUT_SECONDS = 0.05


class ClaimStatus(StrEnum):
    FRESH = "fresh"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    DENIED = "denied"
    DEADLINE = "deadline"


class PublicationStatus(StrEnum):
    PUBLISHED = "published"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"


class CredentialSnapshotError(ValueError):
    pass


class VerificationDeadlineExceeded(TimeoutError):
    pass


@dataclass(frozen=True)
class VerificationAttemptReceipt:
    lease_token: uuid.UUID
    outcome: VerificationOutcome
    error_code: str
    observation_hash: str


@dataclass(frozen=True)
class PublicationReceipt:
    status: PublicationStatus
    accepted_tenant_ids: frozenset = frozenset()


@dataclass(frozen=True)
class VerificationClaim:
    status: ClaimStatus
    requested_tenant_ids: frozenset
    observation: CredentialObservation | None = None
    request: CredentialRequestSnapshot | None = None
    lease_token: uuid.UUID | None = None
    lease_expires_at: timezone.datetime | None = None
    completed_attempt: VerificationAttemptReceipt | None = None


def snapshot_credential(connection, token=None) -> CredentialRequestSnapshot:
    if connection.credential_type == TenantConnection.API_KEY:
        try:
            secret = decrypt_credential(connection.encrypted_credential)
        except InvalidToken as exc:
            raise CredentialSnapshotError("API credential cannot be decoded") from exc
        if not secret:
            raise CredentialSnapshotError("API credential is empty")
        fingerprint = hashlib.sha256(secret.encode()).hexdigest()
        token_snapshot = None
        account_identity = ""
    else:
        if token is None:
            raise ValueError("OAuth connection has no current token")
        secret = token.token
        if not secret:
            raise CredentialSnapshotError("OAuth access token is empty")
        fingerprint = credential_fingerprint(token)
        token_snapshot = (token.id, token.token_secret, token.app_id)
        account_identity = str(token.account_id)
    observation = CredentialObservation(
        connection_id=connection.id,
        user_id=connection.user_id,
        provider=connection.provider,
        credential_type=connection.credential_type,
        credential_fingerprint=fingerprint,
        account_identity=account_identity,
        scope_key=connection.scope_key,
        upstream_denied_at=connection.upstream_denied_at,
    )
    return CredentialRequestSnapshot(observation, secret, token_snapshot)


def _locked_snapshot(
    actor_user_id,
    connection_id,
    *,
    deadline=None,
    clock=time.monotonic,
    cancelled: threading.Event | None = None,
):
    _configure_transaction_deadline(deadline, clock, cancelled)
    User.objects.select_for_update().get(pk=actor_user_id)
    _configure_transaction_deadline(deadline, clock, cancelled)
    initial = TenantConnection.objects.get(pk=connection_id, user_id=actor_user_id)
    token = None
    if initial.credential_type == TenantConnection.OAUTH:
        _configure_transaction_deadline(deadline, clock, cancelled)
        token = (
            SocialToken.objects.select_for_update(of=("self",))
            .select_related("account")
            .filter(
                account_id=initial.social_account_id,
                account__in=provider_accounts(actor_user_id, initial.provider),
            )
            .first()
        )
        if token is None:
            raise ValueError("OAuth connection has no current token")
    _configure_transaction_deadline(deadline, clock, cancelled)
    current = TenantConnection.objects.select_for_update().get(
        pk=connection_id, user_id=actor_user_id
    )
    if token is not None:
        observed_scope = account_scope(token.account)
        if (
            token.account.user_id != actor_user_id
            or canonical_provider(token.account.provider) != canonical_provider(current.provider)
            or current.social_account_id != token.account_id
            or observed_scope != current.scope_key
            or (canonical_provider(current.provider) == "ocs" and not observed_scope)
        ):
            raise ValueError("OAuth identity changed")
    return current, snapshot_credential(current, token)


def proof_is_fresh(proof, observation, *, now=None) -> bool:
    now = now or timezone.now()
    return bool(
        proof.verified_at
        and now - proof.verified_at < PROOF_MAX_AGE
        and proof.credential_fingerprint == observation.credential_fingerprint
        and proof.account_identity == observation.account_identity
        and proof.scope_key == observation.scope_key
        and proof.observed_denied_at == observation.upstream_denied_at
    )


def _observation_hash(observation: CredentialObservation) -> str:
    value = json.dumps(
        [
            str(observation.connection_id),
            observation.user_id,
            observation.provider,
            observation.credential_type,
            observation.credential_fingerprint,
            observation.account_identity,
            observation.scope_key,
            observation.upstream_denied_at.isoformat() if observation.upstream_denied_at else None,
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _completed_attempt(control) -> VerificationAttemptReceipt | None:
    if not control.last_attempt_lease_token or not control.last_attempt_outcome:
        return None
    try:
        outcome = VerificationOutcome(control.last_attempt_outcome)
    except ValueError:
        return None
    return VerificationAttemptReceipt(
        lease_token=control.last_attempt_lease_token,
        outcome=outcome,
        error_code=control.last_attempt_error_code,
        observation_hash=control.last_attempt_observation_hash,
    )


def attempt_receipt_matches(receipt, lease_token, observation) -> bool:
    return bool(
        receipt
        and lease_token
        and observation
        and receipt.lease_token == lease_token
        and receipt.observation_hash == _observation_hash(observation)
    )


def _deadline_expired(deadline, clock) -> bool:
    return deadline is not None and clock() >= deadline


def _ensure_before_deadline(deadline, clock, cancelled=None) -> None:
    if (cancelled is not None and cancelled.is_set()) or _deadline_expired(deadline, clock):
        raise VerificationDeadlineExceeded


def _configure_transaction_deadline(deadline, clock, cancelled=None) -> None:
    _ensure_before_deadline(deadline, clock, cancelled)
    if deadline is None:
        return
    remaining = deadline - clock()
    if django_connection.vendor == "postgresql":
        milliseconds = max(1, int(remaining * 1000))
        with django_connection.cursor() as cursor:
            cursor.execute("SELECT set_config('lock_timeout', %s, true)", [f"{milliseconds}ms"])
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)", [f"{milliseconds}ms"]
            )


def _is_lock_timeout(error: OperationalError) -> bool:
    return getattr(error.__cause__, "sqlstate", None) in {"55P03", "57014"}


def _claim_verification(
    actor_user_id,
    connection_id,
    tenant_ids,
    *,
    now=None,
    deadline=None,
    clock=time.monotonic,
    cancelled=None,
):
    requested = frozenset(tenant_ids)
    if not requested:
        return VerificationClaim(ClaimStatus.DENIED, requested)
    with transaction.atomic():
        try:
            current, request = _locked_snapshot(
                actor_user_id,
                connection_id,
                deadline=deadline,
                clock=clock,
                cancelled=cancelled,
            )
        except (
            User.DoesNotExist,
            TenantConnection.DoesNotExist,
            SocialToken.DoesNotExist,
            ValueError,
        ):
            return VerificationClaim(ClaimStatus.DENIED, requested)
        _configure_transaction_deadline(deadline, clock, cancelled)
        history_rows = list(
            TenantMembership.all_objects.filter(
                user_id=actor_user_id, connection=current, tenant_id__in=requested
            ).values_list(
                "tenant_id",
                "archived_at",
                "tenant__provider",
                "provider_metadata",
            )
        )
        connection_provider = canonical_provider(current.provider)
        scoped_ocs_oauth = (
            connection_provider == "ocs" and current.credential_type == TenantConnection.OAUTH
        )
        history = [
            (tenant_id, archived_at)
            for tenant_id, archived_at, tenant_provider, metadata in history_rows
            if canonical_provider(tenant_provider) == connection_provider
            and (
                not scoped_ocs_oauth
                or (metadata or {}).get("team_slug") in (None, "", current.scope_key)
            )
        ]
        owned = {tenant_id for tenant_id, _archived_at in history}
        if owned != requested:
            return VerificationClaim(ClaimStatus.DENIED, requested)
        all_memberships_live = all(archived_at is None for _tenant_id, archived_at in history)
        _configure_transaction_deadline(deadline, clock, cancelled)
        proofs = {
            proof.tenant_id: proof
            for proof in UpstreamAccessProof.objects.filter(
                connection=current, tenant_id__in=requested
            )
        }
        fresh_now = now or timezone.now()
        if (
            requested
            and all_memberships_live
            and all(
                tenant_id in proofs
                and proof_is_fresh(proofs[tenant_id], request.observation, now=fresh_now)
                for tenant_id in requested
            )
        ):
            _ensure_before_deadline(deadline, clock, cancelled)
            return VerificationClaim(
                ClaimStatus.FRESH, requested, observation=request.observation, request=request
            )
        _configure_transaction_deadline(deadline, clock, cancelled)
        control, _ = VerificationControl.objects.select_for_update().get_or_create(
            connection=current
        )
        _ensure_before_deadline(deadline, clock, cancelled)
        lease_now = now or timezone.now()
        if (
            control.lease_token
            and control.lease_expires_at
            and lease_now < control.lease_expires_at
        ):
            return VerificationClaim(
                ClaimStatus.IN_PROGRESS,
                requested,
                observation=request.observation,
                lease_token=control.lease_token,
                lease_expires_at=control.lease_expires_at,
                completed_attempt=_completed_attempt(control),
            )
        lease_token = uuid.uuid4()
        expires_at = lease_now + LEASE_DURATION
        control.lease_token = lease_token
        control.lease_expires_at = expires_at
        _configure_transaction_deadline(deadline, clock, cancelled)
        control.save(update_fields=["lease_token", "lease_expires_at"])
        _ensure_before_deadline(deadline, clock, cancelled)
        return VerificationClaim(
            ClaimStatus.CLAIMED,
            requested,
            observation=request.observation,
            request=request,
            lease_token=lease_token,
            lease_expires_at=expires_at,
            completed_attempt=_completed_attempt(control),
        )


def claim_verification(
    actor_user_id,
    connection_id,
    tenant_ids,
    *,
    now=None,
    deadline=None,
    clock=time.monotonic,
    cancelled=None,
):
    requested = frozenset(tenant_ids)
    try:
        return _claim_verification(
            actor_user_id,
            connection_id,
            requested,
            now=now,
            deadline=deadline,
            clock=clock,
            cancelled=cancelled,
        )
    except VerificationDeadlineExceeded:
        return VerificationClaim(ClaimStatus.DEADLINE, requested)
    except OperationalError as exc:
        if deadline is not None and _is_lock_timeout(exc):
            return VerificationClaim(ClaimStatus.DEADLINE, requested)
        raise


def release_verification(claim) -> bool:
    if claim.observation is None or claim.lease_token is None:
        return False
    try:
        with transaction.atomic():
            if django_connection.vendor == "postgresql":
                milliseconds = max(1, int(CLEANUP_TIMEOUT_SECONDS * 1000))
                with django_connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('lock_timeout', %s, true)", [f"{milliseconds}ms"]
                    )
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        [f"{milliseconds}ms"],
                    )
            return bool(
                VerificationControl.objects.filter(
                    connection_id=claim.observation.connection_id,
                    lease_token=claim.lease_token,
                ).update(lease_token=None, lease_expires_at=None)
            )
    except OperationalError as exc:
        if _is_lock_timeout(exc):
            return False
        raise


def _same_identity_except_credential(before, after) -> bool:
    return (
        before.connection_id == after.connection_id
        and before.user_id == after.user_id
        and before.provider == after.provider
        and before.credential_type == after.credential_type
        and before.account_identity == after.account_identity
        and before.scope_key == after.scope_key
        and before.upstream_denied_at == after.upstream_denied_at
    )


def _rebase_verification_claim(
    claim, persisted_token, *, now=None, deadline=None, clock=time.monotonic
):
    """Move one live OAuth lease onto the exact token persisted after refresh."""
    if (
        claim.status != ClaimStatus.CLAIMED
        or claim.observation is None
        or claim.request is None
        or claim.lease_token is None
        or claim.request.token_snapshot is None
    ):
        return None
    with transaction.atomic():
        try:
            current, request = _locked_snapshot(
                claim.observation.user_id,
                claim.observation.connection_id,
                deadline=deadline,
                clock=clock,
            )
            _configure_transaction_deadline(deadline, clock)
            control = VerificationControl.objects.select_for_update().get(connection=current)
        except (
            User.DoesNotExist,
            TenantConnection.DoesNotExist,
            SocialToken.DoesNotExist,
            VerificationControl.DoesNotExist,
            ValueError,
        ):
            return None
        _ensure_before_deadline(deadline, clock)
        decision_now = now or timezone.now()
        expected_token_snapshot = (
            persisted_token.token_id,
            persisted_token.refresh_token,
            persisted_token.app_id,
        )
        if (
            control.lease_token != claim.lease_token
            or not control.lease_expires_at
            or decision_now >= control.lease_expires_at
            or not _same_identity_except_credential(claim.observation, request.observation)
            or request.credential != persisted_token.access_token
            or request.token_snapshot != expected_token_snapshot
            or request.observation.account_identity != str(persisted_token.account_id)
        ):
            return None
        return VerificationClaim(
            ClaimStatus.CLAIMED,
            claim.requested_tenant_ids,
            observation=request.observation,
            request=request,
            lease_token=claim.lease_token,
            lease_expires_at=control.lease_expires_at,
        )


def rebase_verification_claim(
    claim, persisted_token, *, now=None, deadline=None, clock=time.monotonic
):
    try:
        return _rebase_verification_claim(
            claim,
            persisted_token,
            now=now,
            deadline=deadline,
            clock=clock,
        )
    except OperationalError as exc:
        if deadline is not None and _is_lock_timeout(exc):
            raise VerificationDeadlineExceeded from exc
        raise


def _publish_verification_receipt(
    claim,
    result: VerificationResult,
    *,
    now=None,
    verified_at=None,
    deadline=None,
    clock=time.monotonic,
):
    if claim.status != ClaimStatus.CLAIMED or claim.observation is None:
        return PublicationReceipt(PublicationStatus.REJECTED)
    with transaction.atomic():
        try:
            current, request = _locked_snapshot(
                claim.observation.user_id,
                claim.observation.connection_id,
                deadline=deadline,
                clock=clock,
            )
            _configure_transaction_deadline(deadline, clock)
            control = VerificationControl.objects.select_for_update().get(connection=current)
        except (
            User.DoesNotExist,
            TenantConnection.DoesNotExist,
            SocialToken.DoesNotExist,
            VerificationControl.DoesNotExist,
            ValueError,
        ):
            # Snapshot failure must not delay a repaired credential. Match the
            # original lease so an obsolete publisher cannot release a successor.
            VerificationControl.objects.filter(
                connection_id=claim.observation.connection_id,
                lease_token=claim.lease_token,
            ).update(lease_token=None, lease_expires_at=None)
            return PublicationReceipt(PublicationStatus.REJECTED)
        _ensure_before_deadline(deadline, clock)
        if control.lease_token != claim.lease_token:
            return PublicationReceipt(PublicationStatus.REJECTED)
        decision_now = now or timezone.now()
        proof_verified_at = min(verified_at or decision_now, decision_now)
        if (
            not control.lease_expires_at
            or decision_now >= control.lease_expires_at
            or request.observation != claim.observation
        ):
            control.lease_token = None
            control.lease_expires_at = None
            control.save(update_fields=["lease_token", "lease_expires_at"])
            return PublicationReceipt(PublicationStatus.REJECTED)
        if (
            result.outcome == VerificationOutcome.TENANT_DENIED
            and result.denied_tenant_id not in claim.requested_tenant_ids
        ):
            control.lease_token = None
            control.lease_expires_at = None
            control.save(update_fields=["lease_token", "lease_expires_at"])
            return PublicationReceipt(PublicationStatus.REJECTED)
        expected_denial_code = {
            VerificationOutcome.TENANT_DENIED: ErrorCode.AUTH_ACCESS_DENIED,
            VerificationOutcome.CREDENTIAL_REJECTED: ErrorCode.AUTH_TOKEN_EXPIRED,
        }.get(result.outcome)
        if expected_denial_code is not None and result.error_code != expected_denial_code:
            control.lease_token = None
            control.lease_expires_at = None
            control.save(update_fields=["lease_token", "lease_expires_at"])
            return PublicationReceipt(PublicationStatus.REJECTED)
        accepted_tenant_ids = frozenset()
        if result.outcome == VerificationOutcome.COMPLETE:
            if current.upstream_denial_code:
                current.upstream_denial_code = ""
                current.save(update_fields=["upstream_denial_code"])
            owned_history = TenantMembership.all_objects.filter(
                user_id=current.user_id,
                connection=current,
                tenant__provider=current.provider,
            )
            scoped_ocs_oauth = (
                canonical_provider(current.provider) == "ocs"
                and current.credential_type == TenantConnection.OAUTH
            )
            returned_memberships = owned_history.filter(tenant_id__in=result.tenant_ids)
            if scoped_ocs_oauth:
                returned_memberships = returned_memberships.filter(
                    Q(provider_metadata__team_slug=current.scope_key)
                    | Q(provider_metadata__team_slug__isnull=True)
                    | Q(provider_metadata__team_slug="")
                )
                returned = list(returned_memberships)
                for membership in returned:
                    membership.archived_at = None
                    membership.provider_metadata = {
                        **(membership.provider_metadata or {}),
                        "team_slug": current.scope_key,
                    }
                    membership.save(update_fields=["archived_at", "provider_metadata"])
                omission_scope = owned_history.filter(
                    provider_metadata__team_slug=current.scope_key
                )
            else:
                returned_memberships.update(archived_at=None)
                returned = list(returned_memberships)
                omission_scope = owned_history
            omitted_ids = list(
                omission_scope.exclude(tenant_id__in=result.tenant_ids).values_list(
                    "tenant_id", flat=True
                )
            )
            omission_scope.filter(archived_at__isnull=True, tenant_id__in=omitted_ids).update(
                archived_at=decision_now
            )
            # Legacy discovery can restore a tombstone without our lease; it must
            # not revive an older positive proof after authoritative omission.
            UpstreamAccessProof.objects.filter(
                connection=current, tenant_id__in=omitted_ids
            ).update(verified_at=None)
            live_ids = {membership.tenant_id for membership in returned}
            accepted_tenant_ids = frozenset(live_ids)
            for tenant_id in live_ids:
                UpstreamAccessProof.objects.update_or_create(
                    connection=current,
                    tenant_id=tenant_id,
                    defaults={
                        "credential_fingerprint": request.observation.credential_fingerprint,
                        "account_identity": request.observation.account_identity,
                        "scope_key": request.observation.scope_key,
                        "observed_denied_at": request.observation.upstream_denied_at,
                        "verified_at": proof_verified_at,
                        "last_attempt_result": result.outcome.value,
                        "last_error_code": result.error_code,
                    },
                )
        elif result.outcome in {
            VerificationOutcome.UNAVAILABLE,
            VerificationOutcome.INDETERMINATE,
        }:
            owned_ids = TenantMembership.all_objects.filter(
                user_id=current.user_id,
                connection=current,
                tenant_id__in=claim.requested_tenant_ids,
            ).values_list("tenant_id", flat=True)
            for tenant_id in owned_ids:
                proof, _ = UpstreamAccessProof.objects.get_or_create(
                    connection=current,
                    tenant_id=tenant_id,
                    defaults={
                        "credential_fingerprint": request.observation.credential_fingerprint,
                        "account_identity": request.observation.account_identity,
                        "scope_key": request.observation.scope_key,
                        "observed_denied_at": request.observation.upstream_denied_at,
                    },
                )
                proof.last_attempt_result = result.outcome.value
                proof.last_error_code = result.error_code
                proof.save(update_fields=["last_attempt_result", "last_error_code"])
        elif result.outcome == VerificationOutcome.TENANT_DENIED:
            denial_count = record_validated_upstream_denial(
                current,
                code=result.error_code,
                tenant_id=result.denied_tenant_id,
                now=decision_now,
            )
            if denial_count is None:
                control.lease_token = None
                control.lease_expires_at = None
                control.save(update_fields=["lease_token", "lease_expires_at"])
                return PublicationReceipt(PublicationStatus.REJECTED)
            UpstreamAccessProof.objects.filter(
                connection=current, tenant_id=result.denied_tenant_id
            ).update(
                last_attempt_result=result.outcome.value,
                last_error_code=result.error_code,
            )
        elif result.outcome == VerificationOutcome.CREDENTIAL_REJECTED:
            denial_count = record_validated_upstream_denial(
                current, code=result.error_code, now=decision_now
            )
            if denial_count is None:
                control.lease_token = None
                control.lease_expires_at = None
                control.save(update_fields=["lease_token", "lease_expires_at"])
                return PublicationReceipt(PublicationStatus.REJECTED)
            UpstreamAccessProof.objects.filter(connection=current).update(
                last_attempt_result=result.outcome.value,
                last_error_code=result.error_code,
            )
        _ensure_before_deadline(deadline, clock)
        control.last_attempt_lease_token = claim.lease_token
        control.last_attempt_outcome = result.outcome.value
        control.last_attempt_error_code = result.error_code
        control.last_attempt_observation_hash = _observation_hash(claim.observation)
        control.lease_token = None
        control.lease_expires_at = None
        control.save(
            update_fields=[
                "last_attempt_lease_token",
                "last_attempt_outcome",
                "last_attempt_error_code",
                "last_attempt_observation_hash",
                "lease_token",
                "lease_expires_at",
            ]
        )
        return PublicationReceipt(PublicationStatus.PUBLISHED, accepted_tenant_ids)


def publish_verification_receipt(
    claim,
    result: VerificationResult,
    *,
    now=None,
    verified_at=None,
    deadline=None,
    clock=time.monotonic,
):
    try:
        return _publish_verification_receipt(
            claim,
            result,
            now=now,
            verified_at=verified_at,
            deadline=deadline,
            clock=clock,
        )
    except VerificationDeadlineExceeded:
        return PublicationReceipt(PublicationStatus.TIMED_OUT)
    except OperationalError as exc:
        if deadline is not None and _is_lock_timeout(exc):
            return PublicationReceipt(PublicationStatus.TIMED_OUT)
        raise


def publish_verification(
    claim,
    result: VerificationResult,
    *,
    now=None,
    verified_at=None,
    deadline=None,
    clock=time.monotonic,
):
    return publish_verification_receipt(
        claim,
        result,
        now=now,
        verified_at=verified_at,
        deadline=deadline,
        clock=clock,
    ).status


aclaim_verification = sync_to_async(claim_verification)
apublish_verification = sync_to_async(publish_verification)
apublish_verification_receipt = sync_to_async(publish_verification_receipt)
arebase_verification_claim = sync_to_async(rebase_verification_claim)
arelease_verification = sync_to_async(release_verification)
