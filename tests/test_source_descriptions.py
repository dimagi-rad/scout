"""Source descriptions: OCS chatbot probe, Connect export text, tenant list, agent prompt (#917)."""

from unittest.mock import AsyncMock

import pytest
from django.test import AsyncClient

from apps.users.models import Tenant, TenantMembership
from apps.users.services.source_descriptions import (
    aprobe_ocs_chatbots,
    arecord_ocs_descriptions,
    connect_description,
)
from apps.users.services.tenant_listing.rows import MAX_DESCRIPTION_CHARS, clean_description


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _session(description, created_at="2026-10-01T09:00:00Z"):
    return {
        "id": "s1",
        "created_at": created_at,
        "experiment": {"id": "x", "name": "Bot", "description": description},
    }


@pytest.fixture
def ocs_sessions(mocker):
    by_experiment = {}

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
    return by_experiment


@pytest.mark.asyncio
@pytest.mark.ocs_description_probe
async def test_probe_reads_each_chatbots_newest_session(ocs_sessions):
    ocs_sessions.update(
        {
            "described": _Response(200, {"results": [_session("Answers maternal health FAQs")]}),
            "silent": _Response(200, {"results": []}),
            "refused": _Response(403, {}),
            "broken": RuntimeError("boom"),
        }
    )

    probes = await aprobe_ocs_chatbots(
        "https://ocs.example.com", {"X-api-key": "k"}, ["described", "silent", "refused", "broken"]
    )

    assert probes["described"].description == "Answers maternal health FAQs"
    assert probes["described"].last_session_at == "2026-10-01T09:00:00Z"
    assert probes["silent"].description is None
    assert probes["silent"].last_session_at is None
    assert "refused" not in probes
    assert "broken" not in probes


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.ocs_description_probe
async def test_recording_stores_found_descriptions_and_keeps_the_rest(ocs_sessions):
    await Tenant.objects.acreate(provider="ocs", external_id="a", canonical_name="A")
    await Tenant.objects.acreate(
        provider="ocs", external_id="b", canonical_name="B", description="Kept"
    )
    ocs_sessions.update(
        {
            "a": _Response(200, {"results": [_session("New text")]}),
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
