from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class VerificationOutcome(StrEnum):
    COMPLETE = "complete"
    CREDENTIAL_REJECTED = "credential_rejected"
    TENANT_DENIED = "tenant_denied"
    UNAVAILABLE = "unavailable"
    INDETERMINATE = "indeterminate"


class AccessVerificationStatus(StrEnum):
    VERIFIED = "verified"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    INDETERMINATE = "indeterminate"
    IN_PROGRESS = "in_progress"
    RETRY = "retry"


@dataclass(frozen=True)
class AccessVerificationResult:
    status: AccessVerificationStatus
    error_code: str = ""


@dataclass(frozen=True)
class CredentialObservation:
    connection_id: UUID
    user_id: int
    provider: str
    credential_type: str
    credential_fingerprint: str
    account_identity: str
    scope_key: str
    upstream_denied_at: datetime | None

    def with_scope(self, scope_key: str) -> CredentialObservation:
        return replace(self, scope_key=scope_key)


@dataclass(frozen=True)
class CredentialRequestSnapshot:
    observation: CredentialObservation
    credential: str = field(repr=False)
    token_snapshot: tuple[int, str, int] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class VerificationResult:
    outcome: VerificationOutcome
    tenant_ids: frozenset[UUID] = frozenset()
    denied_tenant_id: UUID | None = None
    error_code: str = ""

    @classmethod
    def complete(cls, tenant_ids) -> VerificationResult:
        return cls(VerificationOutcome.COMPLETE, frozenset(tenant_ids))

    @classmethod
    def credential_rejected(cls, error_code: str) -> VerificationResult:
        return cls(VerificationOutcome.CREDENTIAL_REJECTED, error_code=error_code)

    @classmethod
    def tenant_denied(cls, tenant_id: UUID, error_code: str) -> VerificationResult:
        return cls(
            VerificationOutcome.TENANT_DENIED,
            denied_tenant_id=tenant_id,
            error_code=error_code,
        )

    @classmethod
    def unavailable(cls, error_code: str) -> VerificationResult:
        return cls(VerificationOutcome.UNAVAILABLE, error_code=error_code)

    @classmethod
    def indeterminate(cls, error_code: str) -> VerificationResult:
        return cls(VerificationOutcome.INDETERMINATE, error_code=error_code)


@dataclass(frozen=True)
class ProviderVerificationResult:
    outcome: VerificationOutcome
    external_ids: frozenset[str] = frozenset()
    denied_external_id: str | None = None
    error_code: str = ""

    @classmethod
    def complete(cls, external_ids) -> ProviderVerificationResult:
        return cls(VerificationOutcome.COMPLETE, frozenset(external_ids))

    @classmethod
    def credential_rejected(cls, error_code: str) -> ProviderVerificationResult:
        return cls(VerificationOutcome.CREDENTIAL_REJECTED, error_code=error_code)

    @classmethod
    def tenant_denied(cls, external_id: str, error_code: str) -> ProviderVerificationResult:
        return cls(
            VerificationOutcome.TENANT_DENIED,
            denied_external_id=external_id,
            error_code=error_code,
        )

    @classmethod
    def unavailable(cls, error_code: str) -> ProviderVerificationResult:
        return cls(VerificationOutcome.UNAVAILABLE, error_code=error_code)

    @classmethod
    def indeterminate(cls, error_code: str) -> ProviderVerificationResult:
        return cls(VerificationOutcome.INDETERMINATE, error_code=error_code)
