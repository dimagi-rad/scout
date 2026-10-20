"""The tenant-list poll refreshes every identity at once and backs off a failing one (#918)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from django.core.cache import cache

from apps.users import views as user_views
from apps.users.views import (
    TENANT_REFRESH_FAILURE_TTL,
    TENANT_REFRESH_TTL,
    _arefresh_all_identities,
)

USER = SimpleNamespace(id=918001)


def _token(provider: str, account_id: int, team: str = "") -> SimpleNamespace:
    uid = f"{account_id}#{team}" if team else str(account_id)
    return SimpleNamespace(
        account_id=account_id,
        account=SimpleNamespace(provider=provider, uid=uid, extra_data={}),
    )


def _key(provider: str, account_id: int) -> str:
    return f"tenant_refresh:{USER.id}:{provider}:{account_id}"


def _ttls(aset) -> dict[str, int]:
    return {call.args[0]: call.args[2] for call in aset.call_args_list}


@pytest.fixture
def tokens():
    return {
        "commcare": [_token("commcare", 1)],
        "ocs": [_token("ocs", 2, team="alpha"), _token("ocs", 3, team="beta")],
    }


@pytest.fixture
def identities(tokens):
    """One CommCare identity and two OCS teams, each provider with its own resolver."""
    resolvers = {"commcare": AsyncMock(), "ocs": AsyncMock()}

    async def tokens_for(_user, provider):
        return tokens.get(provider, [])

    cache.clear()
    with (
        patch.dict(user_views._PROVIDER_RESOLVERS, resolvers, clear=True),
        patch.object(user_views, "aiter_social_tokens", side_effect=tokens_for),
        patch.object(
            user_views, "_aresolve_oauth_credential", AsyncMock(return_value={"value": "tok"})
        ),
        patch.object(user_views, "TENANT_REFRESH_INFLIGHT_POLL", 0.01),
    ):
        yield resolvers
    cache.clear()


@pytest.fixture
def aset():
    with patch.object(user_views.cache, "aset", wraps=user_views.cache.aset) as spy:
        yield spy


@pytest.mark.asyncio
async def test_identities_refresh_concurrently(identities, aset):
    started = 0
    all_started = asyncio.Event()

    async def resolve(*_args, **_kwargs):
        nonlocal started
        started += 1
        if started == 3:
            all_started.set()
        # Times out (and the refresh counts as failed) unless all three are in flight at once.
        await asyncio.wait_for(all_started.wait(), timeout=2)

    identities["commcare"].side_effect = resolve
    identities["ocs"].side_effect = resolve

    await _arefresh_all_identities(USER)

    assert set(_ttls(aset).values()) == {TENANT_REFRESH_TTL}
    assert len(_ttls(aset)) == 3


@pytest.mark.asyncio
async def test_identities_sharing_a_scope_refresh_newest_first_one_at_a_time(
    tokens, identities, aset
):
    # Both OCS identities reach team "alpha"; the first to resolve binds the scope.
    tokens["ocs"] = [_token("ocs", 2, team="alpha"), _token("ocs", 3, team="alpha")]
    order: list[str] = []
    running = 0

    async def resolve(*_args, social_account, **_kwargs):
        nonlocal running
        running += 1
        assert running == 1, "same-scope identities overlapped"
        order.append(social_account.uid)
        await asyncio.sleep(0.01)
        running -= 1

    identities["ocs"].side_effect = resolve

    await _arefresh_all_identities(USER)

    assert order == ["2#alpha", "3#alpha"]


@pytest.mark.asyncio
async def test_failing_identity_backs_off_without_hiding_the_others(identities, aset):
    async def resolve(*_args, social_account, **_kwargs):
        if social_account.uid == "3#beta":
            raise RuntimeError("upstream down")

    identities["ocs"].side_effect = resolve

    await _arefresh_all_identities(USER)

    assert _ttls(aset) == {
        _key("commcare", 1): TENANT_REFRESH_TTL,
        _key("ocs", 2): TENANT_REFRESH_TTL,
        _key("ocs", 3): TENANT_REFRESH_FAILURE_TTL,
    }

    # The next poll inside the failure TTL does not hit the failing upstream again.
    await _arefresh_all_identities(USER)
    assert identities["ocs"].await_count == 2
    assert identities["commcare"].await_count == 1


@pytest.mark.asyncio
async def test_failed_identity_is_retried_once_its_failure_ttl_lapses(identities):
    identities["commcare"].side_effect = RuntimeError("upstream down")
    await _arefresh_all_identities(USER)

    await cache.adelete(_key("commcare", 1))
    identities["commcare"].side_effect = None
    await _arefresh_all_identities(USER)

    assert identities["commcare"].await_count == 2
    assert await cache.aget(_key("commcare", 1))


@pytest.mark.asyncio
async def test_concurrent_poll_waits_for_the_running_fetch_instead_of_repeating_it(identities):
    all_started = asyncio.Event()
    release = asyncio.Event()
    started = 0

    async def slow(*_args, **_kwargs):
        nonlocal started
        started += 1
        if started == 3:
            all_started.set()
        await release.wait()

    identities["commcare"].side_effect = slow
    identities["ocs"].side_effect = slow

    first = asyncio.ensure_future(_arefresh_all_identities(USER))
    await asyncio.wait_for(all_started.wait(), timeout=2)
    second = asyncio.ensure_future(_arefresh_all_identities(USER))
    await asyncio.sleep(0.05)
    # The second poll does not return with the pre-refresh list while the first is fetching.
    assert not second.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

    assert identities["commcare"].await_count == 1
    assert identities["ocs"].await_count == 2


@pytest.mark.asyncio
async def test_forced_refresh_does_not_wait_on_a_running_fetch(identities):
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow(*_args, **_kwargs):
        started.set()
        await release.wait()

    identities["commcare"].side_effect = slow
    first = asyncio.ensure_future(_arefresh_all_identities(USER))
    await asyncio.wait_for(started.wait(), timeout=2)

    # That fetch may predate the access the user is refreshing for.
    identities["commcare"].side_effect = None
    await asyncio.wait_for(_arefresh_all_identities(USER, force=True), timeout=2)
    assert identities["commcare"].await_count == 2

    release.set()
    await first


@pytest.mark.asyncio
async def test_failure_cancels_sibling_refreshes_and_releases_them(identities):
    sibling_cancelled = asyncio.Event()

    async def hang(*_args, **_kwargs):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise

    async def explode(*_args, **_kwargs):
        await asyncio.sleep(0.01)
        raise asyncio.CancelledError

    identities["commcare"].side_effect = hang
    identities["ocs"].side_effect = explode

    with pytest.raises(asyncio.CancelledError):
        await _arefresh_all_identities(USER)

    assert sibling_cancelled.is_set()
    identities["commcare"].side_effect = None
    identities["ocs"].side_effect = None
    await asyncio.wait_for(_arefresh_all_identities(USER), timeout=2)
    assert identities["commcare"].await_count == 2
