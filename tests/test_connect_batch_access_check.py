"""Connect's batch access check (commcare-connect#1588) decides a workspace in one request.

Until Connect serves the route, or whenever it answers in an unexpected way, the
older per-opportunity and full-listing checks decide exactly as they did before.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from uuid import uuid4

import httpx
import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken

from apps.users.models import Tenant, TenantConnection, TenantMembership, UpstreamAccessProof
from apps.users.services.access_verification_providers import verify_provider
from apps.users.services.access_verification_service import verify_connection_access
from apps.users.services.access_verification_types import (
    AccessVerificationStatus,
    CredentialObservation,
    CredentialRequestSnapshot,
    VerificationOutcome,
)
from apps.users.services.tenant_listing.connect import BATCH_MAX_IDS, MAX_SUBSET

BASE = "https://connect.example"
BATCH = f"{BASE}/export/opportunity_access/"
LISTING = f"{BASE}/export/opp_org_program_list/"


def _opp(external_id):
    return f"{BASE}/export/opportunity/{external_id}/"


class _Client:
    """Answers the batch POST with ``batch`` and each GET from ``responses``."""

    def __init__(self, batch, responses=None):
        self.batch = batch
        self.responses = responses or {}
        self.posts = []
        self.gets = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if isinstance(self.batch, Exception):
            raise self.batch
        return self.batch

    async def get(self, url, **kwargs):
        self.gets.append(url)
        return self.responses[url]


def _json(status, payload, url, method="GET"):
    return httpx.Response(status, json=payload, request=httpx.Request(method, url))


def _batch(access, status=200):
    return _json(status, {"opportunities": access}, BATCH, method="POST")


def _snapshot():
    return CredentialRequestSnapshot(
        CredentialObservation(
            connection_id=uuid4(),
            user_id=1,
            provider="commcare_connect",
            credential_type=TenantConnection.OAUTH,
            credential_fingerprint="fingerprint",
            account_identity="account",
            scope_key="",
            upstream_denied_at=None,
        ),
        "secret",
    )


async def _check(settings, external_ids, client):
    settings.CONNECT_API_URL = BASE
    return await verify_provider(
        _snapshot(),
        client_factory=lambda: client,
        settings=settings,
        limiter=asyncio.Semaphore(1),
        external_ids=frozenset(external_ids),
    )


@pytest.mark.asyncio
async def test_one_batch_request_decides_every_opportunity(settings):
    client = _Client(_batch({"7": True, "8": False, "12": True}))

    result = await _check(settings, {"7", "8", "12"}, client)

    assert result.outcome == VerificationOutcome.COMPLETE
    assert set(result.external_ids) == {"7", "12"}
    assert result.scope == frozenset({"7", "8", "12"})
    [(url, kwargs)] = client.posts
    assert url == BATCH
    assert kwargs["json"] == {"opportunity_ids": [12, 7, 8]}
    assert kwargs["headers"]["Authorization"] == "Bearer secret"
    assert kwargs["follow_redirects"] is False
    assert client.gets == []


@pytest.mark.asyncio
async def test_batch_covers_workspaces_too_large_for_per_opportunity_checks(settings):
    ids = {str(100 + i) for i in range(MAX_SUBSET + 1)}
    client = _Client(_batch(dict.fromkeys(ids, True)))

    result = await _check(settings, ids, client)

    assert result.outcome == VerificationOutcome.COMPLETE
    assert set(result.external_ids) == ids
    assert client.gets == []


@pytest.mark.asyncio
async def test_more_ids_than_connect_accepts_use_the_listing(settings):
    ids = {str(100 + i) for i in range(BATCH_MAX_IDS + 1)}
    listed = [{"id": int(i), "name": i} for i in ids]
    client = _Client(None, {LISTING: _json(200, {"opportunities": listed}, LISTING)})

    result = await _check(settings, ids, client)

    assert result.outcome == VerificationOutcome.COMPLETE
    assert client.posts == []
    assert client.gets == [LISTING]


@pytest.mark.parametrize("external_id", ["0", "07", "abc"])
@pytest.mark.asyncio
async def test_ids_connect_would_reject_skip_the_batch(settings, external_id):
    listed = [{"id": 7, "name": "7"}]
    client = _Client(
        None,
        {
            LISTING: _json(200, {"opportunities": listed}, LISTING),
            _opp("7"): _json(200, {"id": 7}, _opp("7")),
            # "0" still routes per opportunity; Connect only rejects it in a batch.
            _opp("0"): _json(404, {"detail": "Not found."}, _opp("0")),
        },
    )

    await _check(settings, {"7", external_id}, client)

    assert client.posts == []


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(
            httpx.Response(404, text="<h1>Not Found</h1>", request=httpx.Request("POST", BATCH)),
            id="route-not-deployed",
        ),
        pytest.param(_json(404, {"detail": "Not found."}, BATCH, "POST"), id="json-404"),
        pytest.param(_json(405, {"detail": "Method not allowed."}, BATCH, "POST"), id="405"),
        pytest.param(_json(400, {"opportunity_ids": ["bad"]}, BATCH, "POST"), id="400"),
        pytest.param(_batch({"7": True}), id="missing-id"),
        pytest.param(_batch({"7": True, "8": "yes"}), id="non-bool"),
        pytest.param(_batch({"7": True, "8": 1}), id="int-not-bool"),
        pytest.param(_batch({"7": True, "8": True, "9": True}), id="extra-id"),
        pytest.param(_json(200, {"unexpected": True}, BATCH, "POST"), id="wrong-shape"),
        pytest.param(
            httpx.Response(200, text="not json", request=httpx.Request("POST", BATCH)),
            id="not-json",
        ),
    ],
)
@pytest.mark.asyncio
async def test_unusable_batch_answer_falls_back_to_per_opportunity_checks(settings, answer):
    client = _Client(
        answer,
        {_opp("7"): _json(200, {"id": 7}, _opp("7")), _opp("8"): _json(200, {"id": 8}, _opp("8"))},
    )

    result = await _check(settings, {"7", "8"}, client)

    assert result.outcome == VerificationOutcome.COMPLETE
    assert set(result.external_ids) == {"7", "8"}
    assert sorted(client.gets) == [_opp("7"), _opp("8")]


@pytest.mark.parametrize(
    ("answer", "cause"),
    [
        pytest.param(
            _json(400, {"opportunity_ids": ["bad"]}, BATCH, "POST"), "batch_http_status", id="400"
        ),
        pytest.param(
            _json(401, {"detail": "Invalid token."}, BATCH, "POST"), "batch_http_status", id="401"
        ),
        pytest.param(
            _json(403, {"detail": "Forbidden."}, BATCH, "POST"), "batch_http_status", id="403"
        ),
        pytest.param(_json(503, {}, BATCH, "POST"), "batch_http_status", id="503"),
        pytest.param(
            httpx.Response(
                302, headers={"location": "/login/"}, request=httpx.Request("POST", BATCH)
            ),
            "batch_http_status",
            id="redirect",
        ),
        pytest.param(httpx.ConnectError("down"), "batch_request_error:ConnectError", id="network"),
        pytest.param(httpx.ReadTimeout("slow"), "batch_request_error:ReadTimeout", id="timeout"),
        pytest.param(_batch({"7": True, "8": True}), "batch_unrecognized_answer", id="extra-id"),
    ],
)
@pytest.mark.asyncio
async def test_batch_never_decides_short_of_a_wellformed_answer(settings, caplog, answer, cause):
    """Whatever an edge or proxy answers for this route, the older checks decide."""
    caplog.set_level(logging.WARNING)
    client = _Client(answer, {_opp("7"): _json(200, {"id": 7}, _opp("7"))})

    result = await _check(settings, {"7"}, client)

    assert result.outcome == VerificationOutcome.COMPLETE
    assert set(result.external_ids) == {"7"}
    assert client.gets == [_opp("7")]
    assert any(f"cause={cause}" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_older_checks_still_reject_a_dead_credential(settings):
    client = _Client(
        _json(401, {"detail": "Invalid token."}, BATCH, "POST"),
        {_opp("7"): _json(401, {"detail": "Invalid token."}, _opp("7"))},
    )

    result = await _check(settings, {"7"}, client)

    assert result.outcome == VerificationOutcome.CREDENTIAL_REJECTED


@pytest.mark.asyncio
async def test_batch_answer_landing_past_the_deadline_is_logged(settings, caplog):
    caplog.set_level(logging.WARNING)
    now = [0.0]

    class SlowBatch(_Client):
        async def post(self, url, **kwargs):
            now[0] = 100.0
            return await super().post(url, **kwargs)

    settings.CONNECT_API_URL = BASE
    result = await verify_provider(
        _snapshot(),
        client_factory=lambda: SlowBatch(_batch({"7": True})),
        settings=settings,
        limiter=asyncio.Semaphore(1),
        external_ids=frozenset({"7"}),
        clock=lambda: now[0],
    )

    assert result.outcome == VerificationOutcome.UNAVAILABLE
    assert any(
        "cause=batch_deadline_after_response" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_undeployed_route_falls_back_without_logging(settings, caplog):
    caplog.set_level(logging.WARNING)
    client = _Client(
        httpx.Response(404, text="<h1>Not Found</h1>", request=httpx.Request("POST", BATCH)),
        {_opp("7"): _json(200, {"id": 7}, _opp("7"))},
    )

    await _check(settings, {"7"}, client)

    assert not [record for record in caplog.records if "batch_" in record.getMessage()]


@pytest.fixture
def connect_setup(db, user, settings):
    settings.CONNECT_API_URL = BASE
    account = SocialAccount.objects.create(user=user, provider="commcare_connect", uid="identity")
    SocialToken.objects.create(account=account, token="access")
    connection = TenantConnection.objects.create(
        user=user,
        provider="commcare_connect",
        credential_type=TenantConnection.OAUTH,
        social_account=account,
    )
    opps = [
        Tenant.objects.create(provider="commcare_connect", external_id=i, canonical_name=i)
        for i in ("7", "8", "9")
    ]
    for tenant in opps:
        TenantMembership.objects.create(user=user, tenant=tenant, connection=connection)
    return connection, opps


async def _is_live(user, tenant):
    return await TenantMembership.objects.filter(user=user, tenant=tenant).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_batch_proves_granted_archives_denied_and_leaves_unasked(user, connect_setup):
    connection, (opp_7, opp_8, opp_9) = connect_setup
    client = _Client(_batch({"7": True, "8": False}))
    provider = partial(verify_provider, client_factory=lambda: client, limiter=asyncio.Semaphore(1))

    result = await verify_connection_access(
        user.id, connection.id, {opp_7.id, opp_8.id}, provider_verifier=provider
    )

    assert result.status == AccessVerificationStatus.DENIED
    assert await _is_live(user, opp_7)
    assert not await _is_live(user, opp_8)
    # Not asked about, so neither proved nor archived.
    assert await _is_live(user, opp_9)
    assert await UpstreamAccessProof.objects.filter(
        connection=connection, tenant=opp_7, verified_at__isnull=False
    ).aexists()
    assert not await UpstreamAccessProof.objects.filter(
        connection=connection, tenant=opp_9
    ).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_batch_granting_everything_verifies(user, connect_setup):
    connection, (opp_7, opp_8, _opp_9) = connect_setup
    client = _Client(_batch({"7": True, "8": True}))
    provider = partial(verify_provider, client_factory=lambda: client, limiter=asyncio.Semaphore(1))

    result = await verify_connection_access(
        user.id, connection.id, {opp_7.id, opp_8.id}, provider_verifier=provider
    )

    assert result.status == AccessVerificationStatus.VERIFIED
    assert client.gets == []
