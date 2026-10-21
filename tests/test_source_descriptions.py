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
    chatbots = None


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
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
        if url.startswith("https://ocs.example.com/api/v2/chatbots/"):
            return by_experiment.chatbots or _Response(200, {"results": [], "next": None})
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
    assert probes["silent"].description is None
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
async def test_tenant_list_gives_an_ocs_chatbot_its_members_team(user, mocker):
    mocker.patch("apps.users.views._arefresh_all_identities", AsyncMock())
    bot = await Tenant.objects.acreate(provider="ocs", external_id="bot", canonical_name="Bot")
    membership = TenantMembership(user=user, tenant=bot)
    membership.team_slug = "maternal"
    membership.team_name = "Maternal Health"
    await membership.asave()
    client = AsyncClient()
    await client.aforce_login(user)

    (row,) = (await client.get("/api/auth/tenants/")).json()

    assert row["attributes"] == {"team": "maternal", "team_name": "Maternal Health"}
    assert (await Tenant.objects.aget(id=bot.id)).provider_attributes == {}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_tenant_list_gives_an_ocs_chatbot_its_last_activity(user, mocker):
    mocker.patch("apps.users.views._arefresh_all_identities", AsyncMock())
    bot = await Tenant.objects.acreate(
        provider="ocs",
        external_id="bot",
        canonical_name="Bot",
        provider_attributes={"last_activity_at": "2026-10-19T14:02:11Z"},
    )
    membership = TenantMembership(user=user, tenant=bot)
    membership.team_slug = "maternal"
    await membership.asave()
    client = AsyncClient()
    await client.aforce_login(user)

    (row,) = (await client.get("/api/auth/tenants/")).json()

    assert row["attributes"]["last_activity_at"] == "2026-10-19T14:02:11Z"


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


async def _api_key_bot(user, external_id="bot", **tenant_fields):
    conn = await TenantConnection.objects.acreate(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("the-key"),
    )
    bot = await Tenant.objects.acreate(
        provider="ocs", external_id=external_id, canonical_name="Bot", **tenant_fields
    )
    await TenantMembership.objects.acreate(user=user, tenant=bot, connection=conn)
    return conn, bot


def _chatbot_row(external_id, last_activity_at):
    return {"id": external_id, "name": "Bot", "last_activity_at": last_activity_at}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_api_key_probe_refreshes_its_chatbots_activity(user, ocs_sessions):
    conn, bot = await _api_key_bot(
        user,
        provider_attributes={
            "last_activity_at": None,
            "activity_checked_at": "2026-10-01T00:00:00+00:00",
        },
    )
    quiet = await Tenant.objects.acreate(
        provider="ocs",
        external_id="quiet",
        canonical_name="Quiet",
        provider_attributes={"last_activity_at": "2026-09-01T00:00:00Z"},
    )
    await TenantMembership.objects.acreate(user=user, tenant=quiet, connection=conn)
    ocs_sessions["bot"] = _Response(200, {"results": []})
    ocs_sessions["quiet"] = _Response(200, {"results": []})
    ocs_sessions.chatbots = _Response(
        200,
        {
            "results": [
                _chatbot_row("bot", "2026-10-20T08:00:00Z"),
                _chatbot_row("not-onboarded", "2026-10-20T08:00:00Z"),
            ],
            "next": None,
        },
    )

    await aprobe_connection_chatbots(str(conn.id), "https://ocs.example.com")

    attributes = (await Tenant.objects.aget(id=bot.id)).provider_attributes
    assert attributes["last_activity_at"] == "2026-10-20T08:00:00Z"
    assert attributes["activity_checked_at"] > "2026-10-01T00:00:00+00:00"
    # Left out of the list: what was stored stays, rather than reading as inactive.
    quiet_attributes = (await Tenant.objects.aget(id=quiet.id)).provider_attributes
    assert quiet_attributes == {"last_activity_at": "2026-09-01T00:00:00Z"}
    assert not await Tenant.objects.filter(external_id="not-onboarded").aexists()
    listing = ocs_sessions.client.get.await_args_list[0]
    assert listing.args[0].startswith("https://ocs.example.com/api/v2/chatbots/")
    assert listing.kwargs["headers"] == {"X-api-key": "the-key"}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_failed_activity_list_still_records_descriptions(user, ocs_sessions):
    conn, bot = await _api_key_bot(user)
    ocs_sessions.chatbots = _Response(500, {})
    ocs_sessions["bot"] = _Response(200, {"results": [_session("bot", "Triage helper")]})

    await aprobe_connection_chatbots(str(conn.id), "https://ocs.example.com")

    stored = await Tenant.objects.aget(id=bot.id)
    assert stored.description == "Triage helper"
    assert stored.provider_attributes == {}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_freshly_listed_key_skips_the_activity_list(user, ocs_sessions):
    checked_at = timezone.now().isoformat()
    conn, _bot = await _api_key_bot(
        user, provider_attributes={"last_activity_at": None, "activity_checked_at": checked_at}
    )
    ocs_sessions["bot"] = _Response(200, {"results": []})

    await aprobe_connection_chatbots(str(conn.id), "https://ocs.example.com")

    (call,) = ocs_sessions.client.get.await_args_list
    assert call.args[0] == "https://ocs.example.com/api/sessions/"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_hourly_sweep_queues_a_probe_for_each_live_ocs_api_key(user, other_user, mocker):
    schedule = mocker.patch("apps.users.tasks.aschedule_ocs_probe", AsyncMock())
    mine, _bot = await _api_key_bot(user)
    theirs, _their_bot = await _api_key_bot(other_user, external_id="their-bot")
    archived, _archived_bot = await _api_key_bot(user, external_id="gone")
    await TenantMembership.all_objects.filter(connection=archived).aupdate(
        archived_at=timezone.now()
    )
    oauth = await TenantConnection.objects.acreate(
        user=user, provider="ocs", credential_type=TenantConnection.OAUTH, scope_key="team"
    )
    commcare = await TenantConnection.objects.acreate(
        user=user,
        provider="commcare",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("other-key"),
    )
    for conn in (oauth, commcare):
        tenant = await Tenant.objects.acreate(
            provider=conn.provider, external_id=f"via-{conn.id}", canonical_name="T"
        )
        await TenantMembership.objects.acreate(user=user, tenant=tenant, connection=conn)

    await users_tasks.schedule_ocs_api_key_probes()

    queued = sorted(str(call.args[0]) for call in schedule.await_args_list)
    assert queued == sorted([str(mine.id), str(theirs.id)])


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
    # OAuth discovery already re-lists the team; the probe must not spend v2 on it.
    assert all(
        call.args[0] == "https://ocs.example.com/api/sessions/"
        for call in ocs_sessions.client.get.await_args_list
    )


@pytest.mark.asyncio
async def test_a_failed_queue_does_not_hold_the_cooldown(monkeypatch):
    monkeypatch.setattr(
        users_tasks.probe_ocs_chatbots, "defer_async", AsyncMock(side_effect=RuntimeError)
    )
    await cache.adelete("ocs_chatbot_probe:conn-2")

    await users_tasks.aschedule_ocs_probe("conn-2")

    assert await cache.aget("ocs_chatbot_probe:conn-2") is None


@pytest.mark.asyncio
async def test_probe_never_sends_a_credential_to_an_unsafe_origin(ocs_sessions):
    assert await aprobe_ocs_chatbots("http://ocs.example.com", {"X-api-key": "k"}, ["a"]) == {}

    ocs_sessions.client.get.assert_not_called()


@pytest.mark.asyncio
async def test_probe_stops_once_ocs_refuses_or_rate_limits(ocs_sessions, monkeypatch):
    monkeypatch.setattr(source_descriptions, "_OCS_PROBE_CONCURRENCY", 1)
    ocs_sessions.update({eid: _Response(429, {}) for eid in ("a", "b", "c")})

    assert await aprobe_ocs_chatbots("https://ocs.example.com", {}, ["a", "b", "c"]) == {}

    assert ocs_sessions.client.get.await_count == 1


@pytest.mark.parametrize(
    "checked", ["2026-02-30T00:00:00Z", "2026-10-21T00:00:00", "soon", 5, None]
)
def test_an_unusable_check_time_reads_as_stale(checked):
    tenant = Tenant(provider_attributes={"activity_checked_at": checked})

    assert not source_descriptions._activity_is_fresh(tenant, timezone.now())
