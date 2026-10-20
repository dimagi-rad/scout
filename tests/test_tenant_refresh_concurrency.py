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


def _token(provider: str, account_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        account_id=account_id,
        account=SimpleNamespace(provider=provider, uid=f"{account_id}#team", extra_data={}),
    )


def _key(provider: str, account_id: int) -> str:
    return f"tenant_refresh:{USER.id}:{provider}:{account_id}"


@pytest.fixture
def identities():
    """Two CommCare identities and one OCS identity, each with its own resolver."""
    tokens = {"commcare": [_token("commcare", 1), _token("commcare", 2)], "ocs": [_token("ocs", 3)]}
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
    ):
        yield resolvers
    cache.clear()


@pytest.mark.asyncio
async def test_identities_refresh_concurrently(identities):
    started = 0
    all_started = asyncio.Event()

    async def resolve(*_args, **_kwargs):
        nonlocal started
        started += 1
        if started == 3:
            all_started.set()
        # Deadlocks (and times out) unless every identity's fetch is in flight at once.
        await asyncio.wait_for(all_started.wait(), timeout=2)

    identities["commcare"].side_effect = resolve
    identities["ocs"].side_effect = resolve

    await _arefresh_all_identities(USER)

    assert started == 3


@pytest.mark.asyncio
async def test_failing_identity_backs_off_without_hiding_the_others(identities):
    identities["ocs"].side_effect = RuntimeError("upstream down")

    with patch.object(user_views.cache, "aset", wraps=user_views.cache.aset) as aset:
        await _arefresh_all_identities(USER)

    ttls = {call.args[0]: call.args[2] for call in aset.call_args_list}
    assert ttls == {
        _key("commcare", 1): TENANT_REFRESH_TTL,
        _key("commcare", 2): TENANT_REFRESH_TTL,
        _key("ocs", 3): TENANT_REFRESH_FAILURE_TTL,
    }
    assert identities["commcare"].await_count == 2

    # The next poll inside the failure TTL does not hit the failing upstream again.
    await _arefresh_all_identities(USER)
    assert identities["ocs"].await_count == 1
    assert identities["commcare"].await_count == 2


@pytest.mark.asyncio
async def test_failed_identity_is_retried_once_its_failure_ttl_lapses(identities):
    identities["ocs"].side_effect = RuntimeError("upstream down")
    await _arefresh_all_identities(USER)

    await cache.adelete(_key("ocs", 3))
    identities["ocs"].side_effect = None
    await _arefresh_all_identities(USER)

    assert identities["ocs"].await_count == 2
    assert await cache.aget(_key("ocs", 3))


@pytest.mark.asyncio
async def test_concurrent_polls_share_one_upstream_fetch(identities):
    release = asyncio.Event()

    async def slow(*_args, **_kwargs):
        await release.wait()

    identities["commcare"].side_effect = slow
    identities["ocs"].side_effect = slow

    first = asyncio.ensure_future(_arefresh_all_identities(USER))
    await asyncio.sleep(0.01)
    # The second poll finds every identity already being refreshed.
    await asyncio.wait_for(_arefresh_all_identities(USER), timeout=2)
    release.set()
    await first

    assert identities["commcare"].await_count == 2
    assert identities["ocs"].await_count == 1


@pytest.mark.asyncio
async def test_cancelled_refresh_releases_its_identity(identities):
    identities["ocs"].side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _arefresh_all_identities(USER)

    identities["ocs"].side_effect = None
    await _arefresh_all_identities(USER)
    assert identities["ocs"].await_count == 2
