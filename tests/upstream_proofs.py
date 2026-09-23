"""Provision genuine upstream-freshness state for tests.

The freshness gate is enforced under test settings, so a member needs what a real
member has: a live membership bound to their own credential and a proof that
credential was verified upstream recently. Nothing here bypasses the authorizer;
the proof fields are exactly what ``proofs_are_fresh`` compares against.

``ProviderStub`` answers the verification adapters' provider calls. It patches the
httpx transport, not ``socket``: async httpx connects through the event loop, which
a socket-level guard never sees (FOLLOW-UPS #2).
"""

import asyncio
import hashlib
from datetime import timedelta

import httpx
from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone

from apps.users.adapters import decrypt_credential, encrypt_credential
from apps.users.models import TenantConnection, TenantMembership, UpstreamAccessProof
from apps.users.services.access_verification import PROOF_MAX_AGE

COMMCARE_DOMAINS_URL = "https://www.commcarehq.org/api/user_domains/v1/"
STALE_AGE = PROOF_MAX_AGE + timedelta(minutes=1)


def api_key_connection(user, provider: str) -> TenantConnection:
    secret = f"{user.pk}:test-key-{provider}"
    connection = TenantConnection.objects.filter(
        user=user, provider=provider, credential_type=TenantConnection.API_KEY
    ).first()
    if connection is None:
        connection = TenantConnection.objects.create(
            user=user,
            provider=provider,
            credential_type=TenantConnection.API_KEY,
            encrypted_credential=encrypt_credential(secret),
        )
    return connection


def record_fresh_proof(connection, tenant, *, verified_at=None) -> UpstreamAccessProof:
    secret = decrypt_credential(connection.encrypted_credential)
    proof, _ = UpstreamAccessProof.objects.update_or_create(
        connection=connection,
        tenant=tenant,
        defaults={
            "credential_fingerprint": hashlib.sha256(secret.encode()).hexdigest(),
            "account_identity": "",
            "scope_key": connection.scope_key,
            "observed_denied_at": connection.upstream_denied_at,
            "verified_at": verified_at or timezone.now(),
            "last_attempt_result": "complete",
        },
    )
    return proof


def grant_fresh_upstream_access(user, tenant, *, verified_at=None) -> TenantMembership:
    """Give ``user`` a live, credential-bound membership with a fresh proof for ``tenant``.

    Bulk writes keep the auto-workspace signal from firing, matching the fixtures
    that call this after creating their own workspace.
    """
    with transaction.atomic():
        return _grant(user, tenant, verified_at=verified_at)


def _grant(user, tenant, *, verified_at):
    connection = api_key_connection(user, tenant.provider)
    TenantMembership.all_objects.bulk_create(
        [TenantMembership(user=user, tenant=tenant, connection=connection)],
        ignore_conflicts=True,
    )
    TenantMembership.all_objects.filter(user=user, tenant=tenant).update(
        connection=connection, archived_at=None
    )
    record_fresh_proof(connection, tenant, verified_at=verified_at)
    return TenantMembership.objects.select_related("user", "tenant", "connection").get(
        user=user, tenant=tenant
    )


agrant_fresh_upstream_access = sync_to_async(grant_fresh_upstream_access)
arecord_fresh_proof = sync_to_async(record_fresh_proof)


def make_proof_stale(user, tenant) -> None:
    UpstreamAccessProof.objects.filter(connection__user=user, tenant=tenant).update(
        verified_at=timezone.now() - STALE_AGE
    )


amake_proof_stale = sync_to_async(make_proof_stale)


class ProviderStub:
    """A CommCare domain listing; set ``domains``, ``failure`` or ``gate`` per test."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.domains: list[str] = []
        self.failure: Exception | int | None = None
        self.gate: asyncio.Event | None = None
        self.requested = asyncio.Event()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.requested.set()
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.failure, Exception):
            raise self.failure
        if isinstance(self.failure, int):
            return httpx.Response(self.failure, request=request)
        assert str(request.url) == COMMCARE_DOMAINS_URL
        body = {
            "meta": {"next": None},
            "objects": [{"domain_name": d, "project_name": d} for d in self.domains],
        }
        return httpx.Response(200, json=body, request=request)

    def install(self, monkeypatch) -> "ProviderStub":
        async def handle_async_request(_transport, request):
            return await self.handle(request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async_request)
        return self
