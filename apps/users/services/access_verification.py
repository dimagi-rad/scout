from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from allauth.socialaccount.models import SocialToken
from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

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
from apps.users.services.oauth_scope import account_scope
from apps.users.services.token_refresh import credential_fingerprint

PROOF_MAX_AGE = timedelta(minutes=5)
LEASE_DURATION = timedelta(seconds=30)


class ClaimStatus(StrEnum):
    FRESH = "fresh"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    DENIED = "denied"


class PublicationStatus(StrEnum):
    PUBLISHED = "published"
    REJECTED = "rejected"


@dataclass(frozen=True)
class VerificationClaim:
    status: ClaimStatus
    requested_tenant_ids: frozenset
    observation: CredentialObservation | None = None
    request: CredentialRequestSnapshot | None = None
    lease_token: uuid.UUID | None = None
    lease_expires_at: timezone.datetime | None = None


def _snapshot(connection, token=None) -> CredentialRequestSnapshot:
    if connection.credential_type == TenantConnection.API_KEY:
        secret = decrypt_credential(connection.encrypted_credential)
        fingerprint = hashlib.sha256(secret.encode()).hexdigest()
        token_snapshot = None
        account_identity = ""
    else:
        if token is None:
            raise ValueError("OAuth connection has no current token")
        secret = token.token
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


def _locked_snapshot(actor_user_id, connection_id):
    User.objects.select_for_update().get(pk=actor_user_id)
    initial = TenantConnection.objects.get(pk=connection_id, user_id=actor_user_id)
    token = None
    if initial.credential_type == TenantConnection.OAUTH:
        token = (
            SocialToken.objects.select_for_update(of=("self",))
            .select_related("account")
            .filter(account_id=initial.social_account_id)
            .first()
        )
        if token is None:
            raise ValueError("OAuth connection has no current token")
    current = TenantConnection.objects.select_for_update().get(
        pk=connection_id, user_id=actor_user_id
    )
    if token is not None and (
        current.social_account_id != token.account_id
        or account_scope(token.account) != current.scope_key
    ):
        raise ValueError("OAuth identity changed")
    return current, _snapshot(current, token)


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


def claim_verification(actor_user_id, connection_id, tenant_ids, *, now=None):
    now = now or timezone.now()
    requested = frozenset(tenant_ids)
    with transaction.atomic():
        try:
            current, request = _locked_snapshot(actor_user_id, connection_id)
        except (
            User.DoesNotExist,
            TenantConnection.DoesNotExist,
            SocialToken.DoesNotExist,
            ValueError,
        ):
            return VerificationClaim(ClaimStatus.DENIED, requested)
        owned = set(
            TenantMembership.all_objects.filter(
                user_id=actor_user_id, connection=current, tenant_id__in=requested
            ).values_list("tenant_id", flat=True)
        )
        if owned != requested:
            return VerificationClaim(ClaimStatus.DENIED, requested)
        proofs = {
            proof.tenant_id: proof
            for proof in UpstreamAccessProof.objects.filter(
                connection=current, tenant_id__in=requested
            )
        }
        if requested and all(
            tenant_id in proofs and proof_is_fresh(proofs[tenant_id], request.observation, now=now)
            for tenant_id in requested
        ):
            return VerificationClaim(
                ClaimStatus.FRESH, requested, observation=request.observation, request=request
            )
        control, _ = VerificationControl.objects.select_for_update().get_or_create(
            connection=current
        )
        if control.lease_token and control.lease_expires_at and now < control.lease_expires_at:
            return VerificationClaim(ClaimStatus.IN_PROGRESS, requested)
        lease_token = uuid.uuid4()
        expires_at = now + LEASE_DURATION
        control.lease_token = lease_token
        control.lease_expires_at = expires_at
        control.save(update_fields=["lease_token", "lease_expires_at"])
        return VerificationClaim(
            ClaimStatus.CLAIMED,
            requested,
            observation=request.observation,
            request=request,
            lease_token=lease_token,
            lease_expires_at=expires_at,
        )


def publish_verification(claim, result: VerificationResult, *, now=None):
    now = now or timezone.now()
    if claim.status != ClaimStatus.CLAIMED or claim.observation is None:
        return PublicationStatus.REJECTED
    with transaction.atomic():
        try:
            current, request = _locked_snapshot(
                claim.observation.user_id, claim.observation.connection_id
            )
            control = VerificationControl.objects.select_for_update().get(connection=current)
        except (
            User.DoesNotExist,
            TenantConnection.DoesNotExist,
            SocialToken.DoesNotExist,
            VerificationControl.DoesNotExist,
            ValueError,
        ):
            return PublicationStatus.REJECTED
        if control.lease_token != claim.lease_token:
            return PublicationStatus.REJECTED
        if (
            not control.lease_expires_at
            or now >= control.lease_expires_at
            or request.observation != claim.observation
        ):
            control.lease_token = None
            control.lease_expires_at = None
            control.save(update_fields=["lease_token", "lease_expires_at"])
            return PublicationStatus.REJECTED
        if (
            result.outcome == VerificationOutcome.TENANT_DENIED
            and result.denied_tenant_id not in claim.requested_tenant_ids
        ):
            control.lease_token = None
            control.lease_expires_at = None
            control.save(update_fields=["lease_token", "lease_expires_at"])
            return PublicationStatus.REJECTED
        if result.outcome == VerificationOutcome.COMPLETE:
            if current.upstream_denial_code:
                current.upstream_denial_code = ""
                current.save(update_fields=["upstream_denial_code"])
            owned_memberships = TenantMembership.all_objects.filter(
                user_id=current.user_id,
                connection=current,
                tenant__provider=current.provider,
            )
            memberships = owned_memberships.filter(
                tenant_id__in=result.tenant_ids,
            )
            memberships.update(archived_at=None)
            owned_memberships.filter(archived_at__isnull=True).exclude(
                tenant_id__in=result.tenant_ids
            ).update(archived_at=now)
            live_ids = set(
                memberships.filter(archived_at__isnull=True).values_list("tenant_id", flat=True)
            )
            for tenant_id in live_ids:
                UpstreamAccessProof.objects.update_or_create(
                    connection=current,
                    tenant_id=tenant_id,
                    defaults={
                        "credential_fingerprint": request.observation.credential_fingerprint,
                        "account_identity": request.observation.account_identity,
                        "scope_key": request.observation.scope_key,
                        "observed_denied_at": request.observation.upstream_denied_at,
                        "verified_at": now,
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
            TenantMembership.all_objects.filter(
                user_id=current.user_id,
                connection=current,
                tenant_id=result.denied_tenant_id,
                archived_at__isnull=True,
            ).update(archived_at=now)
            UpstreamAccessProof.objects.filter(
                connection=current, tenant_id=result.denied_tenant_id
            ).update(
                last_attempt_result=result.outcome.value,
                last_error_code=result.error_code,
            )
            current.upstream_denied_at = now
            current.save(update_fields=["upstream_denied_at"])
        elif result.outcome == VerificationOutcome.CREDENTIAL_REJECTED:
            memberships = TenantMembership.all_objects.filter(
                user_id=current.user_id,
                connection=current,
                tenant__provider=current.provider,
                archived_at__isnull=True,
            )
            if current.provider == "ocs" and current.credential_type == TenantConnection.OAUTH:
                memberships = memberships.filter(
                    Q(provider_metadata__team_slug=current.scope_key)
                    | Q(provider_metadata__team_slug__isnull=True)
                    | Q(provider_metadata__team_slug="")
                )
            memberships.update(archived_at=now)
            UpstreamAccessProof.objects.filter(connection=current).update(
                last_attempt_result=result.outcome.value,
                last_error_code=result.error_code,
            )
            current.upstream_denial_code = result.error_code
            current.upstream_denied_at = now
            current.save(update_fields=["upstream_denial_code", "upstream_denied_at"])
        control.lease_token = None
        control.lease_expires_at = None
        control.save(update_fields=["lease_token", "lease_expires_at"])
        return PublicationStatus.PUBLISHED


aclaim_verification = sync_to_async(claim_verification)
apublish_verification = sync_to_async(publish_verification)
