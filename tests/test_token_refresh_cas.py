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
from apps.users.services.token_refresh import (
    TokenRefreshError,
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
@pytest.mark.parametrize("changed_field", ["scope_key", "upstream_denied_at", "social_account"])
async def test_refresh_cas_rejects_changed_connection_identity_or_denial_fence(
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
            "upstream_denied_at": timezone.now(),
            "social_account": replacement_account,
        }[changed_field]
        await TenantConnection.objects.filter(pk=connection.pk).aupdate(**{changed_field: value})

    def mutate_sync():
        value = {
            "scope_key": "replacement-scope",
            "upstream_denied_at": timezone.now(),
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
async def test_refresh_failure_marker_database_error_is_typed_unavailable(
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
        with pytest.raises(TokenRefreshUnavailable):
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
        with pytest.raises(TokenRefreshUnavailable):
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
        {"access_token": "new", "refresh_token": ""},
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
            raise RuntimeError("database unavailable")

        monkeypatch.setattr("apps.users.services.token_refresh._apersist_refresh_response", fail)
    else:

        def fail(*args, **kwargs):
            raise RuntimeError("database unavailable")

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

    assert elapsed < 0.3
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

    assert elapsed < 0.3
    await connection.arefresh_from_db()
    assert connection.oauth_refresh_failure_fingerprint == ""
