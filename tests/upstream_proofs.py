"""Drive real upstream-freshness rechecks in tests.

``ProviderStub`` answers the verification adapters' provider calls. It patches the
httpx transport, not ``socket``: async httpx connects through the event loop, which
a socket-level guard never sees (FOLLOW-UPS #2).
"""

import asyncio
from datetime import timedelta

import httpx
from django.utils import timezone

from apps.users.models import UpstreamAccessProof
from apps.users.services.access_verification import PROOF_MAX_AGE
from tests.tenant_access import agrant_tenant_access, grant_tenant_access

COMMCARE_DOMAINS_URL = "https://www.commcarehq.org/api/user_domains/v1/"
STALE_AGE = PROOF_MAX_AGE + timedelta(minutes=1)

# Freshness-named aliases: granted tenant access always carries a fresh proof.
grant_fresh_upstream_access = grant_tenant_access
agrant_fresh_upstream_access = agrant_tenant_access


def make_proof_stale(user, tenant) -> None:
    UpstreamAccessProof.objects.filter(connection__user=user, tenant=tenant).update(
        verified_at=timezone.now() - STALE_AGE
    )


async def amake_proof_stale(user, tenant) -> None:
    await UpstreamAccessProof.objects.filter(connection__user=user, tenant=tenant).aupdate(
        verified_at=timezone.now() - STALE_AGE
    )


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
