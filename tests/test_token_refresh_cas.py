from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta

import httpx
import pytest
import requests
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from asgiref.sync import sync_to_async
from django.db import DatabaseError, transaction
from django.db import connection as django_connection
from django.utils import timezone

from apps.users.models import TenantConnection
from apps.users.services import token_refresh
from apps.users.services.token_refresh import (
    TokenRefreshError,
    TokenRefreshRejected,
    TokenRefreshStatus,
    TokenRefreshUnavailable,
    refresh_oauth_token_result,
    refresh_oauth_token_result_sync,
)

URL = "https://provider.example/o/token/"


def _hold_user_lock(user_id, acquired, release):
    try:
        with transaction.atomic():
            user_model = TenantConnection._meta.get_field("user").remote_field.model
            user_model.objects.select_for_update().get(pk=user_id)
            acquired.set()
            assert release.wait(timeout=10)
    finally:
        django_connection.close()


@pytest.fixture
def oauth_identity(user):
    app = SocialApp.objects.create(
        provider="commcare", name="CommCare", client_id="client", secret="app-secret"
    )
    account = SocialAccount.objects.create(user=user, provider="commcare", uid="account")
    expires_at = timezone.now() - timedelta(minutes=1)
    token = SocialToken.objects.create(
        account=account,
        app=app,
        token="old-access",
        token_secret="old-refresh",
        expires_at=expires_at,
    )
    connection = TenantConnection.objects.create(
        user=user,
        provider="commcare",
        credential_type=TenantConnection.OAUTH,
        social_account=account,
    )
    return token, connection


async def _refresh(mode, token, httpx_mock, requests_mock, payload):
    if mode == "async":
        httpx_mock.add_response(url=URL, json=payload)
        return await refresh_oauth_token_result(token, URL)
    requests_mock.post(URL, json=payload)
    return await sync_to_async(refresh_oauth_token_result_sync)(token, URL)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_refresh_result_is_the_persisted_applied_snapshot(
    oauth_identity, mode, httpx_mock, requests_mock
):
    token, _connection = oauth_identity

    result = await _refresh(
        mode,
        token,
        httpx_mock,
        requests_mock,
        {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 900},
    )

    assert result.status == TokenRefreshStatus.APPLIED
    assert result.snapshot.access_token == "new-access"
    assert result.snapshot.refresh_token == "new-refresh"
    persisted = await SocialToken.objects.aget(pk=token.pk)
    assert persisted.token == result.snapshot.access_token
    assert persisted.token_secret == result.snapshot.refresh_token
    assert "new-access" not in repr(result)
    assert "new-refresh" not in repr(result)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_refresh_loser_returns_explicit_superseded_persisted_snapshot(
    oauth_identity, mode, httpx_mock, requests_mock, monkeypatch
):
    token, _connection = oauth_identity
    if mode == "async":
        from apps.users.services import token_refresh

        original = token_refresh._apersist_refresh_response

        async def replace_then_persist(*args, **kwargs):
            await SocialToken.objects.filter(pk=token.pk).aupdate(
                token="winner-access", token_secret="winner-refresh"
            )
            return await original(*args, **kwargs)

        monkeypatch.setattr(token_refresh, "_apersist_refresh_response", replace_then_persist)
    else:
        from apps.users.services import token_refresh

        original = token_refresh._persist_refresh_response

        def replace_then_persist(*args, **kwargs):
            SocialToken.objects.filter(pk=token.pk).update(
                token="winner-access", token_secret="winner-refresh"
            )
            return original(*args, **kwargs)

        monkeypatch.setattr(token_refresh, "_persist_refresh_response", replace_then_persist)

    result = await _refresh(
        mode,
        token,
        httpx_mock,
        requests_mock,
        {"access_token": "discarded-network-secret", "refresh_token": "discarded-refresh"},
    )

    assert result.status == TokenRefreshStatus.SUPERSEDED
    assert result.snapshot.access_token == "winner-access"
    assert result.snapshot.refresh_token == "winner-refresh"
    assert token.token == "winner-access"
    assert "discarded-network-secret" not in repr(result)
    persisted = await SocialToken.objects.aget(pk=token.pk)
    assert persisted.token == "winner-access"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("changed_field", ["scope_key", "social_account"])
async def test_refresh_cas_rejects_changed_connection_identity(
    oauth_identity,
    user,
    mode,
    changed_field,
    httpx_mock,
    requests_mock,
    monkeypatch,
):
    token, connection = oauth_identity
    replacement_account = await SocialAccount.objects.acreate(
        user=user, provider="commcare", uid=f"replacement-{mode}-{changed_field}"
    )

    async def mutate_async():
        value = {
            "scope_key": "replacement-scope",
            "social_account": replacement_account,
        }[changed_field]
        await TenantConnection.objects.filter(pk=connection.pk).aupdate(**{changed_field: value})

    def mutate_sync():
        value = {
            "scope_key": "replacement-scope",
            "social_account": replacement_account,
        }[changed_field]
        TenantConnection.objects.filter(pk=connection.pk).update(**{changed_field: value})

    if mode == "async":
        from apps.users.services import token_refresh

        original = token_refresh._apersist_refresh_response

        async def mutate_then_persist(*args, **kwargs):
            await mutate_async()
            return await original(*args, **kwargs)

        monkeypatch.setattr(token_refresh, "_apersist_refresh_response", mutate_then_persist)
    else:
        from apps.users.services import token_refresh

        original = token_refresh._persist_refresh_response

        def mutate_then_persist(*args, **kwargs):
            mutate_sync()
            return original(*args, **kwargs)

        monkeypatch.setattr(token_refresh, "_persist_refresh_response", mutate_then_persist)

    result = await _refresh(
        mode,
        token,
        httpx_mock,
        requests_mock,
        {"access_token": "discarded-network-secret", "refresh_token": "discarded-refresh"},
    )

    assert result.status == TokenRefreshStatus.SUPERSEDED
    assert result.snapshot.access_token == "old-access"
    assert token.token == "old-access"
    persisted = await SocialToken.objects.aget(pk=token.pk)
    assert persisted.token == "old-access"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_refresh_failure_marker_rejects_newer_denial_fence(
    oauth_identity, mode, httpx_mock, requests_mock, monkeypatch
):
    token, connection = oauth_identity
    if mode == "async":
        from apps.users.services import token_refresh

        original = token_refresh._apersist_refresh_failure

        async def deny_then_record(*args, **kwargs):
            await TenantConnection.objects.filter(pk=connection.pk).aupdate(
                upstream_denied_at=timezone.now()
            )
            return await original(*args, **kwargs)

        monkeypatch.setattr(token_refresh, "_apersist_refresh_failure", deny_then_record)
        httpx_mock.add_response(url=URL, status_code=503)
        with pytest.raises(TokenRefreshUnavailable):
            await refresh_oauth_token_result(token, URL)
    else:
        from apps.users.services import token_refresh

        original = token_refresh._persist_refresh_failure

        def deny_then_record(*args, **kwargs):
            TenantConnection.objects.filter(pk=connection.pk).update(
                upstream_denied_at=timezone.now()
            )
            return original(*args, **kwargs)

        monkeypatch.setattr(token_refresh, "_persist_refresh_failure", deny_then_record)
        requests_mock.post(URL, status_code=503)
        with pytest.raises(TokenRefreshUnavailable):
            await sync_to_async(refresh_oauth_token_result_sync)(token, URL)

    await connection.arefresh_from_db()
    assert connection.oauth_refresh_failure_fingerprint == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("failure", ["rejected", "transport"])
async def test_refresh_failure_marker_error_preserves_provider_classification(
    oauth_identity, mode, failure, httpx_mock, requests_mock, monkeypatch
):
    token, _connection = oauth_identity
    if mode == "async":

        async def fail_persistence(*args, **kwargs):
            raise DatabaseError("database unavailable")

        monkeypatch.setattr(
            "apps.users.services.token_refresh._apersist_refresh_failure", fail_persistence
        )
        if failure == "rejected":
            httpx_mock.add_response(url=URL, status_code=400, json={"error": "invalid_grant"})
        else:
            httpx_mock.add_exception(httpx.ConnectError("offline"), url=URL)
        with pytest.raises(
            TokenRefreshRejected if failure == "rejected" else TokenRefreshUnavailable
        ):
            await refresh_oauth_token_result(token, URL)
    else:

        def fail_persistence(*args, **kwargs):
            raise DatabaseError("database unavailable")

        monkeypatch.setattr(
            "apps.users.services.token_refresh._persist_refresh_failure", fail_persistence
        )
        if failure == "rejected":
            requests_mock.post(URL, status_code=400, json={"error": "invalid_grant"})
        else:
            requests_mock.post(URL, exc=requests.ConnectionError("offline"))
        with pytest.raises(
            TokenRefreshRejected if failure == "rejected" else TokenRefreshUnavailable
        ):
            await sync_to_async(refresh_oauth_token_result_sync)(token, URL)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"access_token": ""},
        {"access_token": 12},
        {"access_token": "new", "refresh_token": 123},
        {"access_token": "new", "expires_in": -1},
    ],
)
async def test_malformed_refresh_response_never_mutates_token(
    oauth_identity, mode, payload, httpx_mock, requests_mock
):
    token, _connection = oauth_identity

    with pytest.raises(TokenRefreshError):
        await _refresh(mode, token, httpx_mock, requests_mock, payload)

    persisted = await SocialToken.objects.aget(pk=token.pk)
    assert persisted.token == "old-access"
    assert persisted.token_secret == "old-refresh"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_persistence_failure_is_typed_unavailable_and_does_not_expose_network_token(
    oauth_identity, mode, httpx_mock, requests_mock, monkeypatch
):
    token, _connection = oauth_identity
    if mode == "async":

        async def fail(*args, **kwargs):
            raise DatabaseError("database unavailable")

        monkeypatch.setattr("apps.users.services.token_refresh._apersist_refresh_response", fail)
    else:

        def fail(*args, **kwargs):
            raise DatabaseError("database unavailable")

        monkeypatch.setattr("apps.users.services.token_refresh._persist_refresh_response", fail)

    with pytest.raises(TokenRefreshUnavailable) as caught:
        await _refresh(
            mode,
            token,
            httpx_mock,
            requests_mock,
            {"access_token": "must-not-escape", "refresh_token": "also-secret"},
        )

    assert "must-not-escape" not in str(caught.value)
    assert token.token == "old-access"
    persisted = await SocialToken.objects.aget(pk=token.pk)
    assert persisted.token == "old-access"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_refresh_success_cannot_persist_after_database_deadline(
    oauth_identity, mode, httpx_mock, requests_mock
):
    token, connection = oauth_identity
    acquired = threading.Event()
    release = threading.Event()
    locker = threading.Thread(target=_hold_user_lock, args=(connection.user_id, acquired, release))
    locker.start()
    assert await asyncio.to_thread(acquired.wait, 2)
    timer = threading.Timer(0.5, release.set)
    timer.start()
    deadline = time.monotonic() + 0.1
    payload = {"access_token": "late-access", "refresh_token": "late-refresh"}
    if mode == "async":
        httpx_mock.add_response(url=URL, json=payload)
        operation = refresh_oauth_token_result(token, URL, deadline=deadline)
    else:
        requests_mock.post(URL, json=payload)
        operation = sync_to_async(refresh_oauth_token_result_sync)(token, URL, deadline=deadline)

    started = time.monotonic()
    with pytest.raises(TokenRefreshUnavailable):
        await operation
    elapsed = time.monotonic() - started
    release.set()
    await asyncio.to_thread(locker.join, 2)
    timer.cancel()

    # Generous: a broken deadline is caught by the raises/row assertions, not the clock.
    # A tight bound only measures CI load (sync_to_async hand-off, a fresh PG connection).
    assert elapsed < 2
    persisted = await SocialToken.objects.aget(pk=token.pk)
    assert persisted.token == "old-access"
    assert persisted.token_secret == "old-refresh"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_refresh_failure_marker_cannot_persist_after_database_deadline(
    oauth_identity, mode, httpx_mock, requests_mock
):
    token, connection = oauth_identity
    acquired = threading.Event()
    release = threading.Event()
    locker = threading.Thread(target=_hold_user_lock, args=(connection.user_id, acquired, release))
    locker.start()
    assert await asyncio.to_thread(acquired.wait, 2)
    timer = threading.Timer(0.5, release.set)
    timer.start()
    deadline = time.monotonic() + 0.1
    if mode == "async":
        httpx_mock.add_response(url=URL, status_code=503)
        operation = refresh_oauth_token_result(token, URL, deadline=deadline)
    else:
        requests_mock.post(URL, status_code=503)
        operation = sync_to_async(refresh_oauth_token_result_sync)(token, URL, deadline=deadline)

    started = time.monotonic()
    with pytest.raises(TokenRefreshUnavailable):
        await operation
    elapsed = time.monotonic() - started
    release.set()
    await asyncio.to_thread(locker.join, 2)
    timer.cancel()

    # Generous: a broken deadline is caught by the raises/row assertions, not the clock.
    # A tight bound only measures CI load (sync_to_async hand-off, a fresh PG connection).
    assert elapsed < 2
    await connection.arefresh_from_db()
    assert connection.oauth_refresh_failure_fingerprint == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_invalid_stable_binding_fails_before_provider_rotation(
    oauth_identity, mode, monkeypatch
):

    token, connection = oauth_identity
    await TenantConnection.objects.filter(pk=connection.pk).aupdate(scope_key="wrong-account-scope")
    if mode == "async":

        async def unexpected(*args, **kwargs):
            pytest.fail("invalid binding must not consume an upstream refresh grant")

        monkeypatch.setattr(httpx.AsyncClient, "post", unexpected)
        with pytest.raises(TokenRefreshError, match="reconnect"):
            await refresh_oauth_token_result(token, URL)
    else:

        def unexpected(*args, **kwargs):
            pytest.fail("invalid binding must not consume an upstream refresh grant")

        monkeypatch.setattr(token_refresh.requests, "post", unexpected)
        with pytest.raises(TokenRefreshError, match="reconnect"):
            await sync_to_async(refresh_oauth_token_result_sync)(token, URL)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_unrelated_api_key_binding_does_not_poison_oauth_cas(
    oauth_identity, mode, httpx_mock, requests_mock
):
    token, connection = oauth_identity
    await TenantConnection.objects.acreate(
        user_id=connection.user_id,
        provider="commcare",
        credential_type=TenantConnection.API_KEY,
        scope_key="api-key",
        social_account_id=token.account_id,
    )
    result = await _refresh(mode, token, httpx_mock, requests_mock, {"access_token": "new-access"})
    assert result.status == TokenRefreshStatus.APPLIED


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("empty_refresh", [None, ""])
async def test_nonrotating_refresh_response_preserves_existing_refresh_token(
    oauth_identity, mode, empty_refresh, httpx_mock, requests_mock
):
    token, _ = oauth_identity
    result = await _refresh(
        mode,
        token,
        httpx_mock,
        requests_mock,
        {"access_token": "new-access", "refresh_token": empty_refresh},
    )
    assert result.status == TokenRefreshStatus.APPLIED
    assert result.snapshot.refresh_token == "old-refresh"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_invalid_grant_stays_terminal_when_failure_recording_is_unavailable(
    oauth_identity, mode, httpx_mock, requests_mock, monkeypatch
):

    token, _ = oauth_identity
    if mode == "async":

        async def unavailable(*args, **kwargs):
            raise DatabaseError("test persistence failure")

        monkeypatch.setattr(token_refresh, "_apersist_refresh_failure", unavailable)
        httpx_mock.add_response(url=URL, status_code=400, json={"error": "invalid_grant"})
        with pytest.raises(token_refresh.TokenRefreshRejected):
            await refresh_oauth_token_result(token, URL)
    else:

        def unavailable(*args, **kwargs):
            raise DatabaseError("test persistence failure")

        monkeypatch.setattr(token_refresh, "_persist_refresh_failure", unavailable)
        requests_mock.post(URL, status_code=400, json={"error": "invalid_grant"})
        with pytest.raises(token_refresh.TokenRefreshRejected):
            await sync_to_async(refresh_oauth_token_result_sync)(token, URL)


@pytest.mark.django_db(transaction=True)
def test_nested_refresh_preflight_restores_callers_timeouts(oauth_identity):
    from apps.users.services.token_refresh import _preflight_token

    token, _ = oauth_identity
    with transaction.atomic():
        with django_connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('lock_timeout', '13s', true), set_config('statement_timeout', '17s', true)"
            )
        _preflight_token(token, deadline=time.monotonic() + 1)
        with django_connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
            )
            assert cursor.fetchone() == ("13s", "17s")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_lost_race_that_did_not_advance_credential_is_not_reported_as_refreshed(
    oauth_identity, mode, httpx_mock, requests_mock, monkeypatch
):
    """A fence-only change strands the rotated-away token; callers must not get it back."""
    token, connection = oauth_identity
    original = token_refresh._lock_refresh_context

    def fence_moves_under_us(preflight, **kwargs):
        current, connections = original(preflight, **kwargs)
        TenantConnection.objects.filter(pk=connection.pk).update(scope_key="replacement-scope")
        return current, list(TenantConnection.objects.filter(pk__in=[c.pk for c in connections]))

    monkeypatch.setattr(token_refresh, "_lock_refresh_context", fence_moves_under_us)

    result = await _refresh(mode, token, httpx_mock, requests_mock, {"access_token": "new-access"})

    assert result.status == TokenRefreshStatus.SUPERSEDED
    assert result.credential_advanced is False
    assert result.snapshot.access_token == "old-access"

    # Stranded, so it must be surfaced as needing reconnect rather than left looking
    # connected while every future refresh fails.
    await connection.arefresh_from_db()
    assert connection.oauth_refresh_failure_fingerprint != ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_stable_fence_violation_is_terminal_rather_than_permanently_superseded(
    oauth_identity, mode
):
    """The CAS must not silently no-op forever on an invariant it can never satisfy.

    It must also fail before the grant is spent, so no HTTP mock is registered here.
    """
    token, connection = oauth_identity
    await TenantConnection.objects.filter(pk=connection.pk).aupdate(scope_key="stale-scope")

    with pytest.raises(TokenRefreshError, match="reconnect"):
        if mode == "async":
            await refresh_oauth_token_result(token, URL)
        else:
            await sync_to_async(refresh_oauth_token_result_sync)(token, URL)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_denial_landing_mid_refresh_does_not_discard_the_rotated_credential(
    oauth_identity, mode, httpx_mock, requests_mock, monkeypatch
):
    """A routine single-tenant 403 must not cost us a token pair the provider issued.

    The endpoints here rotate refresh tokens, so discarding the new pair leaves the
    stored refresh token dead upstream and the connection unable to ever refresh.
    """
    token, connection = oauth_identity
    original = token_refresh._lock_refresh_context

    def deny_under_us(preflight, **kwargs):
        current, connections = original(preflight, **kwargs)
        TenantConnection.objects.filter(pk=connection.pk).update(upstream_denied_at=timezone.now())
        return current, list(TenantConnection.objects.filter(pk__in=[c.pk for c in connections]))

    monkeypatch.setattr(token_refresh, "_lock_refresh_context", deny_under_us)

    result = await _refresh(
        mode,
        token,
        httpx_mock,
        requests_mock,
        {"access_token": "new-access", "refresh_token": "new-refresh"},
    )

    assert result.status == TokenRefreshStatus.APPLIED
    persisted = await SocialToken.objects.aget(pk=token.pk)
    assert persisted.token == "new-access"
    assert persisted.token_secret == "new-refresh"
    # The denial itself must survive the write.
    await connection.arefresh_from_db()
    assert connection.upstream_denied_at is not None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_defect_in_persistence_is_not_downgraded_into_a_dropped_expected_state(
    oauth_identity, mode, httpx_mock, requests_mock, monkeypatch
):
    """config.sentry.before_send drops ExpectedStateError, so a bug must not become one."""
    token, _connection = oauth_identity
    if mode == "async":

        async def boom(*args, **kwargs):
            raise TypeError("refactor left a bad call")

        monkeypatch.setattr("apps.users.services.token_refresh._apersist_refresh_response", boom)
    else:

        def boom(*args, **kwargs):
            raise TypeError("refactor left a bad call")

        monkeypatch.setattr("apps.users.services.token_refresh._persist_refresh_response", boom)

    with pytest.raises(TypeError, match="refactor left a bad call"):
        await _refresh(mode, token, httpx_mock, requests_mock, {"access_token": "new-access"})


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_disconnect_between_preflight_and_persist_is_superseded_not_unavailable(
    oauth_identity, mode, httpx_mock, requests_mock, monkeypatch
):
    """The account vanishing mid-refresh is the race the CAS exists for, not an outage."""
    token, _connection = oauth_identity
    original = token_refresh._lock_refresh_context

    def disconnect_under_us(preflight, **kwargs):
        raise SocialToken.DoesNotExist

    monkeypatch.setattr(token_refresh, "_lock_refresh_context", disconnect_under_us)
    assert original is not token_refresh._lock_refresh_context

    result = await _refresh(mode, token, httpx_mock, requests_mock, {"access_token": "new-access"})

    assert result.status == TokenRefreshStatus.SUPERSEDED
    assert result.credential_advanced is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_failure_marker_write_error_does_not_erase_the_real_classification(
    oauth_identity, mode, httpx_mock, requests_mock, monkeypatch
):
    """A lost marker must not turn a reportable defect into a dropped expected state."""
    token, _connection = oauth_identity
    if mode == "async":

        async def unavailable(*args, **kwargs):
            raise DatabaseError("marker write failed")

        monkeypatch.setattr(token_refresh, "_apersist_refresh_failure", unavailable)
        httpx_mock.add_exception(httpx.ConnectError("offline"), url=URL)
        with pytest.raises(TokenRefreshUnavailable):
            await refresh_oauth_token_result(token, URL)
    else:

        def unavailable(*args, **kwargs):
            raise DatabaseError("marker write failed")

        monkeypatch.setattr(token_refresh, "_persist_refresh_failure", unavailable)
        requests_mock.post(URL, exc=requests.ConnectionError("offline"))
        with pytest.raises(TokenRefreshUnavailable):
            await sync_to_async(refresh_oauth_token_result_sync)(token, URL)
