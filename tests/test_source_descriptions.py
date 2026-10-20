"""Source descriptions: OCS chatbot probe, Connect export text, tenant list, agent prompt (#917)."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from django.core.cache import cache
from django.test import AsyncClient
from django.utils import timezone

from apps.users import tasks as users_tasks
from apps.users.adapters import encrypt_credential
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services import source_descriptions
from apps.users.services.source_descriptions import (
    aprobe_connection_chatbots,
    aprobe_ocs_chatbots,
    arecord_ocs_descriptions,
    connect_description,
)
from apps.users.services.tenant_listing.rows import MAX_DESCRIPTION_CHARS, clean_description


class _Sessions(dict):
    """Each experiment's canned response, plus the mocked client for assertions."""

    client = None


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _session(experiment_id, description, created_at="2026-10-01T09:00:00Z"):
    return {
        "id": "s1",
        "created_at": created_at,
        "experiment": {"id": experiment_id, "name": "Bot", "description": description},
    }


@pytest.fixture
def ocs_sessions(mocker):
    by_experiment = _Sessions()

    async def fake_get(url, headers=None, params=None, **_):
        assert url == "https://ocs.example.com/api/sessions/"
        assert params["page_size"] == 1
        result = by_experiment[params["experiment"]]
        if isinstance(result, Exception):
            raise result
        return result

    client = mocker.MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=fake_get)
    mocker.patch("httpx.AsyncClient", return_value=client)
    by_experiment.client = client
    return by_experiment


@pytest.mark.asyncio
async def test_probe_reads_each_chatbots_newest_session(ocs_sessions):
    ocs_sessions.update(
        {
            "described": _Response(
                200, {"results": [_session("described", "Answers maternal health FAQs")]}
            ),
            "elsewhere": _Response(200, {"results": [_session("someone-else", "Wrong bot")]}),
            "silent": _Response(200, {"results": []}),
            "refused": _Response(403, {}),
            "broken": RuntimeError("boom"),
        }
    )

    probes = await aprobe_ocs_chatbots(
        "https://ocs.example.com",
        {"X-api-key": "k"},
        ["described", "silent", "refused", "broken", "elsewhere"],
    )

    assert probes["described"].description == "Answers maternal health FAQs"
    assert probes["described"].last_session_at == "2026-10-01T09:00:00Z"
    assert probes["silent"].description is None
    assert probes["silent"].last_session_at is None
    assert "refused" not in probes
    assert "broken" not in probes
    assert "elsewhere" not in probes


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_recording_stores_found_descriptions_and_keeps_the_rest(ocs_sessions):
    await Tenant.objects.acreate(provider="ocs", external_id="a", canonical_name="A")
    await Tenant.objects.acreate(
        provider="ocs", external_id="b", canonical_name="B", description="Kept"
    )
    ocs_sessions.update(
        {
            "a": _Response(200, {"results": [_session("a", "New text")]}),
            "b": _Response(200, {"results": []}),
        }
    )

    await arecord_ocs_descriptions("https://ocs.example.com", {"X-api-key": "k"}, ["a", "b"])

    assert (await Tenant.objects.aget(external_id="a")).description == "New text"
    assert (await Tenant.objects.aget(external_id="b")).description == "Kept"


def test_connect_description_prefers_the_full_text():
    assert connect_description({"description": "Full", "short_description": "Short"}) == "Full"
    assert connect_description({"description": "", "short_description": "Short"}) == "Short"
    assert connect_description(None) is None


def test_clean_description_drops_what_postgres_rejects():
    assert clean_description("a\x00b ") == "ab"
    assert clean_description("\ud800") is None
    assert clean_description(42) is None
    assert len(clean_description("x" * (MAX_DESCRIPTION_CHARS + 50))) == MAX_DESCRIPTION_CHARS


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_tenant_list_includes_the_description(user, tenant, mocker):
    mocker.patch("apps.users.views._arefresh_all_identities", AsyncMock())
    await Tenant.objects.filter(id=tenant.id).aupdate(description="A CommCare project")
    await TenantMembership.objects.aget_or_create(user=user, tenant=tenant)
    client = AsyncClient()
    await client.aforce_login(user)

    (row,) = (await client.get("/api/auth/tenants/")).json()

    assert row["description"] == "A CommCare project"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_connection_probe_uses_the_connections_own_api_key(user, ocs_sessions):
    conn = await TenantConnection.objects.acreate(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("the-key"),
    )
    bot = await Tenant.objects.acreate(provider="ocs", external_id="bot", canonical_name="Bot")
    await TenantMembership.objects.acreate(user=user, tenant=bot, connection=conn)
    ocs_sessions["bot"] = _Response(200, {"results": [_session("bot", "Triage helper")]})

    await aprobe_connection_chatbots(str(conn.id), "https://ocs.example.com")

    assert (await Tenant.objects.aget(id=bot.id)).description == "Triage helper"
    headers = ocs_sessions.client.get.await_args.kwargs["headers"]
    assert headers == {"X-api-key": "the-key"}


@pytest.mark.asyncio
async def test_probe_gives_up_at_its_budget(ocs_sessions, monkeypatch):
    monkeypatch.setattr(source_descriptions, "_OCS_PROBE_BUDGET_SECONDS", 0.05)
    hang = asyncio.Event()

    async def never_answers(*_args, **_kwargs):
        await hang.wait()

    ocs_sessions.client.get = AsyncMock(side_effect=never_answers)

    assert await aprobe_ocs_chatbots("https://ocs.example.com", {}, ["a", "b"]) == {}


@pytest.mark.asyncio
async def test_scheduling_queues_one_probe_per_cooldown(monkeypatch):
    queued = AsyncMock()
    monkeypatch.setattr(users_tasks.probe_ocs_chatbots, "defer_async", queued)
    await cache.adelete("ocs_chatbot_probe:conn-1")

    await users_tasks.aschedule_ocs_probe("conn-1")
    await users_tasks.aschedule_ocs_probe("conn-1")

    queued.assert_awaited_once_with(connection_id="conn-1")


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_connection_probe_never_renews_a_near_expiry_oauth_token(user, ocs_sessions, mocker):
    app = await SocialApp.objects.acreate(provider="ocs", name="OCS", client_id="c", secret="s")
    account = await SocialAccount.objects.acreate(user=user, provider="ocs", uid="42#team")
    token = await SocialToken.objects.acreate(
        account=account, app=app, token="t", token_secret="r", expires_at=timezone.now()
    )
    conn = await TenantConnection.objects.acreate(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.OAUTH,
        social_account=account,
        scope_key="team",
    )
    bot = await Tenant.objects.acreate(provider="ocs", external_id="bot", canonical_name="Bot")
    await TenantMembership.objects.acreate(user=user, tenant=bot, connection=conn)
    renew = mocker.patch("apps.users.services.credential_resolver.refresh_oauth_token")

    await aprobe_connection_chatbots(str(conn.id), "https://ocs.example.com")

    renew.assert_not_called()
    ocs_sessions.client.get.assert_not_called()

    token.expires_at = timezone.now() + timedelta(hours=1)
    await token.asave(update_fields=["expires_at"])
    ocs_sessions["bot"] = _Response(200, {"results": [_session("bot", "Fresh")]})

    await aprobe_connection_chatbots(str(conn.id), "https://ocs.example.com")

    assert ocs_sessions.client.get.await_args.kwargs["headers"] == {"Authorization": "Bearer t"}


@pytest.mark.asyncio
async def test_a_failed_queue_does_not_hold_the_cooldown(monkeypatch):
    monkeypatch.setattr(
        users_tasks.probe_ocs_chatbots, "defer_async", AsyncMock(side_effect=RuntimeError)
    )
    await cache.adelete("ocs_chatbot_probe:conn-2")

    await users_tasks.aschedule_ocs_probe("conn-2")

    assert await cache.aget("ocs_chatbot_probe:conn-2") is None
