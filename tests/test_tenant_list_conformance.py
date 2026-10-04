"""Every entry path that reads an upstream tenant list must read it the same way.

CommCare user domains, OCS experiments and Connect opportunities are each listed by
OAuth discovery, API-key onboarding and access verification. The callers keep
different policies (discovery archives omissions, verification is stricter about
identities, onboarding maps failures to form errors), but none may mistake an
unfinished or untrusted listing for a complete one: page 2 past a relative
``next`` is a real membership (#328), and a ``next`` off the configured origin
would carry the credential elsewhere (R23).

Cases marked ``xfail(strict=True)`` are gaps in one path that the shared
paginator closes; they flip to passing, and so fail here, when it lands.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from django.conf import settings as django_settings

from apps.common.commcare_servers import COMMCARE_SERVERS
from apps.common.errors import CommCareAuthError, ConnectAuthError, OCSAuthError
from apps.users.models import Tenant, TenantConnection, TenantMembership, User
from apps.users.services.access_verification_providers import verify_provider
from apps.users.services.access_verification_types import (
    CredentialObservation,
    CredentialRequestSnapshot,
    VerificationOutcome,
)
from apps.users.services.api_key_providers.base import CredentialVerificationError
from apps.users.services.api_key_providers.commcare import CommCareStrategy
from apps.users.services.api_key_providers.ocs import OCSStrategy
from apps.users.services.tenant_resolution import (
    resolve_commcare_domains,
    resolve_connect_opportunities,
    resolve_ocs_chatbots,
)

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).parent / "fixtures" / "tenant_lists"
OCS_ORIGIN = "https://ocs.example.org"
CONNECT_ORIGIN = "https://connect.example.org"
CONNECT_LISTING = f"{CONNECT_ORIGIN}/export/opp_org_program_list/"
OCS_TEAM = "team-a"
TOKEN = "oauth-token"
API_USERNAME = "analyst@example.org"
API_KEY = "api-key"
INDETERMINATE = VerificationOutcome.INDETERMINATE
UNAVAILABLE = VerificationOutcome.UNAVAILABLE

# Where each path reads its wall clock; verification takes an injected clock instead.
_RESOLUTION_CLOCK = "apps.users.services.tenant_resolution"
_COMMCARE_KEY_CLOCK = "apps.users.services.api_key_providers.commcare"


@dataclass(frozen=True)
class EntryPath:
    id: str
    family: str
    kind: str
    server: str = ""
    credential_type: str = TenantConnection.OAUTH
    clock_module: str | None = None

    def __str__(self) -> str:
        return self.id


COMMCARE_PATHS = [
    EntryPath("commcare-oauth-discovery", "commcare", "discovery", clock_module=_RESOLUTION_CLOCK),
    EntryPath(
        "commcare-eu-oauth-discovery",
        "commcare",
        "discovery",
        server="eu",
        clock_module=_RESOLUTION_CLOCK,
    ),
    EntryPath(
        "commcare-api-key",
        "commcare",
        "api_key",
        credential_type=TenantConnection.API_KEY,
        clock_module=_COMMCARE_KEY_CLOCK,
    ),
    EntryPath(
        "commcare-eu-api-key",
        "commcare",
        "api_key",
        server="eu",
        credential_type=TenantConnection.API_KEY,
        clock_module=_COMMCARE_KEY_CLOCK,
    ),
    EntryPath("commcare-oauth-verification", "commcare", "verification"),
    EntryPath("commcare-eu-oauth-verification", "commcare", "verification", server="eu"),
    EntryPath(
        "commcare-api-key-verification",
        "commcare",
        "verification",
        credential_type=TenantConnection.API_KEY,
    ),
]
OCS_PATHS = [
    EntryPath("ocs-oauth-discovery", "ocs", "discovery", clock_module=_RESOLUTION_CLOCK),
    EntryPath("ocs-api-key", "ocs", "api_key", credential_type=TenantConnection.API_KEY),
    EntryPath("ocs-oauth-verification", "ocs", "verification"),
    EntryPath(
        "ocs-api-key-verification",
        "ocs",
        "verification",
        credential_type=TenantConnection.API_KEY,
    ),
]
PAGINATED_PATHS = COMMCARE_PATHS + OCS_PATHS
CONNECT_PATHS = [
    EntryPath("connect-oauth-discovery", "connect", "discovery"),
    EntryPath("connect-oauth-verification", "connect", "verification"),
]

_OCS_KEY_UNBOUNDED = "OCS API-key listing has no wall-clock budget"
_OCS_KEY_RAW_ERRORS = "OCS API-key listing lets shape drift escape as a non-form error"
_OCS_KEY_PARTIAL = "OCS API-key listing reads a page without results as an empty page"
_OCS_KEY_TRANSPORT = "OCS API-key listing lets a transport error escape as a non-form error"
_CONNECT_DISCOVERY_PAGES = "Connect discovery would read only page 1 of a paginated envelope"
KNOWN_GAPS = {
    ("ocs-api-key", "deadline"): _OCS_KEY_UNBOUNDED,
    ("ocs-api-key", "list-not-an-array"): _OCS_KEY_RAW_ERRORS,
    ("ocs-api-key", "not-an-object"): _OCS_KEY_RAW_ERRORS,
    ("ocs-api-key", "not-json"): _OCS_KEY_RAW_ERRORS,
    ("ocs-api-key", "missing-list-on-page-2"): _OCS_KEY_PARTIAL,
    ("ocs-api-key", "transport-page-1"): _OCS_KEY_TRANSPORT,
    ("ocs-api-key", "transport-page-2"): _OCS_KEY_TRANSPORT,
    ("connect-oauth-discovery", "paginated-envelope"): _CONNECT_DISCOVERY_PAGES,
}


# Discovery cases need a transactional database, so they cover the protocol matrix
# more sparsely; the archive-on-omission guard is what they add.
_EU_DISCOVERY = frozenset({"saved", "other-commcare-server", "401-page-2"})


def cases(paths, variants=("",), *, discovery=None):
    for path in paths:
        for variant in variants:
            if path.kind == "discovery":
                allowed = _EU_DISCOVERY if path.server else discovery
                if allowed is not None and variant not in allowed:
                    continue
            marks = []
            if path.kind == "discovery":
                marks.append(pytest.mark.django_db(transaction=True))
            if gap := KNOWN_GAPS.get((path.id, variant)):
                marks.append(pytest.mark.xfail(strict=True, reason=gap))
            yield pytest.param(
                path, variant, id=f"{path.id}-{variant}" if variant else path.id, marks=marks
            )


def saved(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text().replace("{origin}", OCS_ORIGIN))


def origin(path: EntryPath) -> str:
    if path.family == "commcare":
        return COMMCARE_SERVERS[path.server].base_url
    return OCS_ORIGIN if path.family == "ocs" else CONNECT_ORIGIN


def listing_url(path: EntryPath) -> str:
    if path.family == "commcare":
        return COMMCARE_SERVERS[path.server].user_domains_url
    return f"{OCS_ORIGIN}/api/experiments/" if path.family == "ocs" else CONNECT_LISTING


def page_two_url(path: EntryPath) -> str:
    if path.family == "commcare":
        return f"{listing_url(path)}?limit=2&offset=2"
    return f"{listing_url(path)}?cursor=cD0yMDI2"


_FIXTURE_STEM = {"commcare": "user_domains", "ocs": "experiments"}


def first_page(path: EntryPath) -> dict:
    return saved(f"{path.family}_{_FIXTURE_STEM[path.family]}_page_1")


def second_page(path: EntryPath) -> dict:
    return saved(f"{path.family}_{_FIXTURE_STEM[path.family]}_page_2")


def with_next(path: EntryPath, payload: dict, next_value) -> dict:
    page = json.loads(json.dumps(payload))
    if path.family == "commcare":
        page["meta"]["next"] = next_value
    else:
        page["next"] = next_value
    return page


def row(path: EntryPath, external_id: str, name: str) -> dict:
    if path.family == "commcare":
        return {"domain_name": external_id, "project_name": name}
    return {"id": external_id, "name": name}


def make_page(path: EntryPath, rows: list[dict], next_value) -> dict:
    if path.family == "commcare":
        return {"meta": {"next": next_value, "previous": None}, "objects": rows}
    return {"next": next_value, "previous": None, "results": rows}


def ids_of(path: EntryPath, *pages: dict) -> frozenset[str]:
    key, id_key = ("objects", "domain_name") if path.family == "commcare" else ("results", "id")
    return frozenset(str(entry[id_key]) for page in pages for entry in page[key])


def json_answer(payload, status: int = 200):
    return lambda request: httpx.Response(status, json=payload)


def text_answer(text: str, status: int = 200):
    return lambda request: httpx.Response(status, text=text)


def unreachable(request):
    raise httpx.ConnectError("connection refused", request=request)


@dataclass
class Upstream:
    """Serves saved pages to every client and records which URLs were asked for."""

    routes: dict[str, Callable] = field(default_factory=dict)
    fallback: Callable | None = None
    requests: list[str] = field(default_factory=list)
    now: float = 1_000.0
    seconds_per_request: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/sessions/":
            return httpx.Response(200, json={"results": []})
        url = str(request.url)
        self.requests.append(url)
        self.now += self.seconds_per_request
        answer = self.routes.get(url) or self.fallback
        if answer is None:
            return httpx.Response(404, text="unrouted")
        return answer(request)


@pytest.fixture
def upstream(httpx_mock, settings):
    settings.OCS_URL = OCS_ORIGIN
    settings.CONNECT_API_URL = CONNECT_ORIGIN
    served = Upstream()
    httpx_mock.add_callback(served.handle, is_reusable=True, is_optional=True)
    return served


def serve_pages(upstream: Upstream, path: EntryPath, first=None, second=None) -> None:
    upstream.routes[listing_url(path)] = json_answer(first_page(path) if first is None else first)
    upstream.routes[page_two_url(path)] = json_answer(
        second_page(path) if second is None else second
    )


@dataclass
class Outcome:
    ids: frozenset[str] | None = None
    scope: frozenset[str] | None = None
    error: Exception | None = None
    verdict: VerificationOutcome | None = None
    standing_active: bool | None = None
    denied: bool | None = None


_ALLAUTH_PROVIDER = {"ocs": "ocs", "connect": "commcare_connect"}
_TENANT_PROVIDER = {"commcare": "commcare", "ocs": "ocs", "connect": "commcare_connect"}
_RESOLVERS = {
    "commcare": resolve_commcare_domains,
    "ocs": resolve_ocs_chatbots,
    "connect": resolve_connect_opportunities,
}


def _scope_key(path: EntryPath) -> str:
    if path.family == "commcare":
        return path.server
    if path.family == "ocs" and path.credential_type == TenantConnection.OAUTH:
        return OCS_TEAM
    return ""


async def _discover(path: EntryPath) -> Outcome:
    """Run OAuth discovery over a connection that already holds a 'standing' tenant."""
    user = await User.objects.acreate(email="member@example.org")
    if path.family == "commcare":
        allauth_provider = "commcare_eu" if path.server else "commcare"
    else:
        allauth_provider = _ALLAUTH_PROVIDER[path.family]
    uid = f"u1#{OCS_TEAM}" if path.family == "ocs" else f"{allauth_provider}-1"
    account = await SocialAccount.objects.acreate(user=user, provider=allauth_provider, uid=uid)
    await SocialToken.objects.acreate(account=account, token=TOKEN)
    provider = _TENANT_PROVIDER[path.family]
    connection = await TenantConnection.objects.acreate(
        user=user,
        provider=provider,
        credential_type=TenantConnection.OAUTH,
        scope_key=_scope_key(path),
        social_account=account,
    )
    standing = await Tenant.objects.acreate(
        provider=provider, server=path.server, external_id="standing", canonical_name="Standing"
    )
    metadata = {"team_slug": OCS_TEAM, "team_name": "Team A"} if path.family == "ocs" else {}
    await TenantMembership.all_objects.acreate(
        user=user, tenant=standing, connection=connection, provider_metadata=metadata
    )

    outcome = Outcome()
    try:
        await _RESOLVERS[path.family](user, TOKEN, social_account=account)
    except Exception as error:
        outcome.error = error
    else:
        outcome.ids = frozenset(
            [
                external_id
                async for external_id in TenantMembership.objects.filter(
                    user=user, connection=connection, archived_at__isnull=True
                ).values_list("tenant__external_id", flat=True)
            ]
        )
    outcome.standing_active = await TenantMembership.all_objects.filter(
        user=user, tenant=standing, archived_at__isnull=True
    ).aexists()
    connection = await TenantConnection.objects.aget(pk=connection.pk)
    outcome.denied = connection.upstream_denied_at is not None
    return outcome


async def _onboard(path: EntryPath) -> Outcome:
    outcome = Outcome()
    try:
        if path.family == "commcare":
            # The key strategy answers for one named domain; the last page's proves
            # that the whole listing was read.
            descriptors = await CommCareStrategy.verify_and_discover(
                {
                    "server": path.server,
                    "domain": "late-page",
                    "username": API_USERNAME,
                    "api_key": API_KEY,
                }
            )
        else:
            descriptors = await OCSStrategy.verify_and_discover({"api_key": API_KEY})
    except Exception as error:
        outcome.error = error
    else:
        outcome.ids = frozenset(descriptor.external_id for descriptor in descriptors)
    return outcome


async def _verify(path: EntryPath, upstream: Upstream, external_ids) -> Outcome:
    if path.credential_type == TenantConnection.OAUTH:
        credential = TOKEN
    else:
        credential = f"{API_USERNAME}:{API_KEY}" if path.family == "commcare" else API_KEY
    snapshot = CredentialRequestSnapshot(
        CredentialObservation(
            connection_id=uuid4(),
            user_id=1,
            provider=_TENANT_PROVIDER[path.family],
            credential_type=path.credential_type,
            credential_fingerprint="fingerprint",
            account_identity="account" if path.credential_type == TenantConnection.OAUTH else "",
            scope_key=_scope_key(path),
            upstream_denied_at=None,
        ),
        credential,
    )
    result = await verify_provider(
        snapshot,
        clock=upstream.monotonic,
        settings=django_settings,
        limiter=asyncio.Semaphore(1),
        external_ids=frozenset(external_ids),
    )
    if result.outcome != VerificationOutcome.COMPLETE:
        return Outcome(verdict=result.outcome)
    return Outcome(ids=result.external_ids, scope=result.scope, verdict=result.outcome)


async def run(path: EntryPath, upstream: Upstream, *, external_ids=()) -> Outcome:
    clock = (
        patch(f"{path.clock_module}.time", SimpleNamespace(monotonic=upstream.monotonic))
        if path.clock_module
        else nullcontext()
    )
    with clock:
        if path.kind == "discovery":
            return await _discover(path)
        if path.kind == "api_key":
            return await _onboard(path)
        return await _verify(path, upstream, external_ids)


def assert_listed(path: EntryPath, outcome: Outcome, expected: frozenset[str]) -> None:
    assert outcome.error is None, repr(outcome.error)
    if path.kind == "api_key" and path.family == "commcare":
        assert outcome.ids == {"late-page"}
    else:
        assert outcome.ids == expected
    if path.kind == "verification":
        assert outcome.scope is None


def assert_rejected(path: EntryPath, outcome: Outcome, verdict=INDETERMINATE) -> None:
    assert outcome.ids is None, f"{path} accepted an unfinished listing: {sorted(outcome.ids)}"
    if path.kind == "discovery":
        assert outcome.error is not None
        assert outcome.standing_active, "an unfinished listing archived a standing membership"
    elif path.kind == "api_key":
        assert isinstance(outcome.error, CredentialVerificationError), repr(outcome.error)
    else:
        assert outcome.verdict == verdict


NEXT_FORMS = ("saved", "relative-path", "query-only", "absolute", "http-same-host")


def next_reference(path: EntryPath, form: str) -> str:
    target = urlsplit(page_two_url(path))
    return {
        "saved": None,
        "relative-path": f"{target.path}?{target.query}",
        "query-only": f"?{target.query}",
        "absolute": page_two_url(path),
        # Connect's proxy emits http links for its https origin; the policy upgrades them.
        "http-same-host": page_two_url(path).replace("https://", "http://", 1),
    }[form]


@pytest.mark.parametrize(
    ("path", "form"), cases(PAGINATED_PATHS, NEXT_FORMS, discovery={"saved", "query-only"})
)
async def test_every_page_is_read(path, form, upstream):
    first = first_page(path)
    if form != "saved":
        first = with_next(path, first, next_reference(path, form))
    serve_pages(upstream, path, first=first)

    outcome = await run(path, upstream)

    assert_listed(path, outcome, ids_of(path, first, second_page(path)))
    assert upstream.requests == [listing_url(path), page_two_url(path)]


FOREIGN_NEXT = (
    "other-host",
    "scheme-relative",
    "other-port",
    "userinfo",
    "loopback-http",
    "non-http-scheme",
)


def foreign_next(path: EntryPath, kind: str) -> str:
    host = urlsplit(origin(path)).hostname
    if kind == "other-commcare-server":
        other = next(key for key in COMMCARE_SERVERS if key != path.server)
        return f"{COMMCARE_SERVERS[other].user_domains_url}?offset=2"
    return {
        "other-host": "https://attacker.example/collect/?offset=2",
        "scheme-relative": "//attacker.example/collect/",
        "other-port": f"https://{host}:8443/collect/",
        "userinfo": f"https://{host}@attacker.example/collect/",
        "loopback-http": "http://127.0.0.1:8000/collect/",
        "non-http-scheme": "file:///etc/passwd",
    }[kind]


@pytest.mark.parametrize(
    ("path", "target"),
    [
        *cases(
            COMMCARE_PATHS,
            (*FOREIGN_NEXT, "other-commcare-server"),
            discovery={"other-host", "userinfo", "other-commcare-server"},
        ),
        *cases(OCS_PATHS, FOREIGN_NEXT, discovery={"other-host", "userinfo"}),
    ],
)
async def test_next_outside_the_origin_is_never_followed(path, target, upstream):
    serve_pages(upstream, path, first=with_next(path, first_page(path), foreign_next(path, target)))

    outcome = await run(path, upstream)

    assert_rejected(path, outcome)
    assert upstream.requests == [listing_url(path)]


@pytest.mark.parametrize(
    ("path", "shape"),
    cases(PAGINATED_PATHS, ("self-loop", "back-to-first"), discovery={"back-to-first"}),
)
async def test_pagination_cycle_is_rejected(path, shape, upstream):
    first_path = urlsplit(listing_url(path)).path
    if shape == "self-loop":
        serve_pages(upstream, path, first=with_next(path, first_page(path), first_path))
    else:
        serve_pages(upstream, path, second=with_next(path, second_page(path), first_path))

    outcome = await run(path, upstream)

    assert_rejected(path, outcome)
    assert len(upstream.requests) <= 2


def endless(path: EntryPath, upstream: Upstream):
    base = urlsplit(listing_url(path)).path

    def answer(request):
        index = len(upstream.requests)
        rows = [row(path, f"tenant-{index}", f"Tenant {index}")]
        return httpx.Response(200, json=make_page(path, rows, f"{base}?page={index + 1}"))

    return answer


@pytest.mark.parametrize(("path", "variant"), cases(PAGINATED_PATHS))
async def test_endless_pagination_stops_at_the_page_cap(path, variant, upstream):
    upstream.fallback = endless(path, upstream)

    outcome = await run(path, upstream)

    assert_rejected(path, outcome)
    assert 1 < len(upstream.requests) <= 100


@pytest.mark.parametrize(("path", "variant"), cases(PAGINATED_PATHS, ("deadline",)))
async def test_listing_that_outlives_its_budget_is_rejected(path, variant, upstream):
    upstream.fallback = endless(path, upstream)
    # Longer than any path's whole budget, so no path may start a third page.
    upstream.seconds_per_request = 40.0

    outcome = await run(path, upstream)

    assert_rejected(path, outcome, verdict=UNAVAILABLE)
    assert len(upstream.requests) <= 2


MALFORMED = (
    "missing-list",
    "list-not-an-array",
    "not-an-object",
    "not-json",
    "missing-list-on-page-2",
    "next-not-a-string",
)


def serve_malformed(upstream: Upstream, path: EntryPath, variant: str) -> None:
    list_key = "objects" if path.family == "commcare" else "results"
    last = make_page(path, [row(path, "only", "Only")], None)
    if variant == "missing-list":
        last.pop(list_key)
        serve_pages(upstream, path, first=last)
    elif variant == "list-not-an-array":
        last[list_key] = row(path, "only", "Only")
        serve_pages(upstream, path, first=last)
    elif variant == "not-an-object":
        serve_pages(upstream, path, first=[row(path, "only", "Only")])
    elif variant == "not-json":
        upstream.routes[listing_url(path)] = text_answer("<html>Down for maintenance</html>")
    elif variant == "missing-list-on-page-2":
        last.pop(list_key)
        serve_pages(upstream, path, second=last)
    elif variant == "next-not-a-string":
        serve_pages(upstream, path, first=with_next(path, first_page(path), 2))


@pytest.mark.parametrize(
    ("path", "variant"),
    cases(
        PAGINATED_PATHS,
        MALFORMED,
        discovery={"missing-list", "not-json", "missing-list-on-page-2", "next-not-a-string"},
    ),
)
async def test_malformed_page_is_never_read_as_fewer_tenants(path, variant, upstream):
    serve_malformed(upstream, path, variant)

    outcome = await run(path, upstream)

    assert_rejected(path, outcome)


@pytest.mark.parametrize(("path", "variant"), cases(PAGINATED_PATHS))
async def test_conflicting_duplicate_identity(path, variant, upstream):
    """Rejecting one id under two names is verification's policy, not the protocol's."""
    first = first_page(path)
    duplicate = sorted(ids_of(path, first))[0]
    second = make_page(
        path, [row(path, duplicate, "Renamed"), *_rows(path, second_page(path))], None
    )
    serve_pages(upstream, path, first=first, second=second)

    outcome = await run(path, upstream)

    if path.kind == "verification":
        assert_rejected(path, outcome)
    else:
        assert_listed(path, outcome, ids_of(path, first, second))


def _rows(path: EntryPath, page: dict) -> list[dict]:
    return page["objects" if path.family == "commcare" else "results"]


FAILURES = tuple(
    f"{failure}-page-{page}"
    for failure in ("401", "403", "500", "503", "transport")
    for page in (1, 2)
)


def serve_failure(upstream: Upstream, path: EntryPath, variant: str) -> None:
    failure, _, page = variant.rpartition("-page-")
    answer = unreachable if failure == "transport" else json_answer({"detail": "x"}, int(failure))
    if page == "1":
        upstream.routes[listing_url(path)] = answer
    else:
        serve_pages(upstream, path)
        upstream.routes[page_two_url(path)] = answer


_AUTH_ERRORS = {"commcare": CommCareAuthError, "ocs": OCSAuthError, "connect": ConnectAuthError}


def assert_failure_mapping(path: EntryPath, outcome: Outcome, failure: str) -> None:
    """Each caller's existing mapping: only a credential verdict may revoke or reject."""
    assert outcome.ids is None, f"{path} accepted a partial listing: {sorted(outcome.ids)}"
    if path.kind == "discovery":
        if failure in ("401", "403"):
            assert isinstance(outcome.error, _AUTH_ERRORS[path.family]), repr(outcome.error)
            assert outcome.error.status_code == int(failure)
            # Connect's export list can refuse scope without revoking membership.
            assert outcome.denied is not (path.family == "connect" and failure == "403")
            # A recorded denial is what revokes; nothing else about the listing may.
            assert outcome.standing_active is not outcome.denied
        else:
            expected = httpx.ConnectError if failure == "transport" else httpx.HTTPStatusError
            assert isinstance(outcome.error, expected), repr(outcome.error)
            assert not outcome.denied
            assert outcome.standing_active, "an upstream failure archived a standing membership"
    elif path.kind == "api_key":
        assert isinstance(outcome.error, CredentialVerificationError), repr(outcome.error)
        message = {
            "401": "rejected the API key",
            "403": "rejected the API key",
            "500": "unexpected status",
            "503": "unexpected status",
            "transport": "could not be reached",
        }[failure]
        assert message in str(outcome.error)
    else:
        assert (
            outcome.verdict
            == {
                "401": VerificationOutcome.CREDENTIAL_REJECTED,
                "403": INDETERMINATE,
                "500": UNAVAILABLE,
                "503": UNAVAILABLE,
                "transport": UNAVAILABLE,
            }[failure]
        )


@pytest.mark.parametrize(
    ("path", "variant"),
    cases(
        PAGINATED_PATHS,
        FAILURES,
        discovery={"401-page-1", "403-page-2", "503-page-2", "transport-page-2"},
    ),
)
async def test_upstream_failure_keeps_each_callers_mapping(path, variant, upstream):
    serve_failure(upstream, path, variant)

    outcome = await run(path, upstream)

    assert_failure_mapping(path, outcome, variant.rpartition("-page-")[0])


CONNECT_IDS = frozenset({"101", "102"})


def connect_opportunity_url(external_id: str) -> str:
    return f"{CONNECT_ORIGIN}/export/opportunity/{external_id}/"


@pytest.mark.parametrize(("path", "variant"), cases(CONNECT_PATHS))
async def test_connect_listing_is_read_whole(path, variant, upstream):
    upstream.routes[CONNECT_LISTING] = json_answer(saved("connect_opp_org_program_list"))

    outcome = await run(path, upstream)

    assert_listed(path, outcome, CONNECT_IDS)
    assert upstream.requests == [CONNECT_LISTING]


CONNECT_MALFORMED = ("missing-list", "not-an-object", "not-json", "paginated-envelope")


@pytest.mark.parametrize(("path", "variant"), cases(CONNECT_PATHS, CONNECT_MALFORMED))
async def test_connect_malformed_listing_is_rejected(path, variant, upstream):
    listing = saved("connect_opp_org_program_list")
    if variant == "missing-list":
        listing.pop("opportunities")
        upstream.routes[CONNECT_LISTING] = json_answer(listing)
    elif variant == "not-an-object":
        upstream.routes[CONNECT_LISTING] = json_answer(listing["opportunities"])
    elif variant == "not-json":
        upstream.routes[CONNECT_LISTING] = text_answer("<html>Down for maintenance</html>")
    else:
        listing["next"] = f"{CONNECT_LISTING}?page=2"
        upstream.routes[CONNECT_LISTING] = json_answer(listing)

    outcome = await run(path, upstream)

    assert_rejected(path, outcome)


@pytest.mark.parametrize(
    ("path", "variant"), cases(CONNECT_PATHS, ("401", "403", "500", "503", "transport"))
)
async def test_connect_failure_keeps_each_callers_mapping(path, variant, upstream):
    upstream.routes[CONNECT_LISTING] = (
        unreachable if variant == "transport" else json_answer({"detail": "x"}, int(variant))
    )

    outcome = await run(path, upstream)

    assert_failure_mapping(path, outcome, variant)


CONNECT_VERIFICATION = CONNECT_PATHS[1]


async def test_connect_few_opportunities_are_checked_one_by_one(upstream, user):
    upstream.routes[connect_opportunity_url("101")] = json_answer(saved("connect_opportunity_101"))

    outcome = await run(CONNECT_VERIFICATION, upstream, external_ids={"101"})

    assert (outcome.ids, outcome.scope) == ({"101"}, {"101"})
    assert upstream.requests == [connect_opportunity_url("101")]


async def test_connect_no_access_answer_is_an_omission_within_the_scope(upstream, user):
    upstream.routes[connect_opportunity_url("101")] = json_answer(saved("connect_opportunity_101"))
    upstream.routes[connect_opportunity_url("555")] = json_answer(
        saved("connect_opportunity_not_found"), 404
    )

    outcome = await run(CONNECT_VERIFICATION, upstream, external_ids={"101", "555"})

    assert (outcome.ids, outcome.scope) == ({"101"}, {"101", "555"})
    assert CONNECT_LISTING not in upstream.requests


async def test_connect_routing_404_falls_back_to_the_whole_listing(upstream, user):
    upstream.routes[connect_opportunity_url("101")] = text_answer("<html>Not Found</html>", 404)
    upstream.routes[CONNECT_LISTING] = json_answer(saved("connect_opp_org_program_list"))

    outcome = await run(CONNECT_VERIFICATION, upstream, external_ids={"101"})

    assert (outcome.ids, outcome.scope) == (CONNECT_IDS, None)
    assert upstream.requests == [connect_opportunity_url("101"), CONNECT_LISTING]


async def test_connect_many_opportunities_use_the_whole_listing(upstream, user):
    upstream.routes[CONNECT_LISTING] = json_answer(saved("connect_opp_org_program_list"))
    wanted = {str(external_id) for external_id in range(101, 107)}

    outcome = await run(CONNECT_VERIFICATION, upstream, external_ids=wanted)

    assert (outcome.ids, outcome.scope) == (CONNECT_IDS, None)
    assert upstream.requests == [CONNECT_LISTING]


async def test_connect_rejected_token_outranks_the_scoped_check(upstream, user):
    upstream.routes[connect_opportunity_url("101")] = json_answer({"detail": "x"}, 401)

    outcome = await run(CONNECT_VERIFICATION, upstream, external_ids={"101"})

    assert outcome.verdict == VerificationOutcome.CREDENTIAL_REJECTED
    assert CONNECT_LISTING not in upstream.requests
