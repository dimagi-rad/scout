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
