from datetime import UTC, datetime

import pytest

from apps.users.services.api_key_providers import CredentialVerificationError, TenantDescriptor
from apps.users.services.api_key_providers.ocs import OCSStrategy
from apps.users.services.tenant_listing import ocs as ocs_listing


def test_form_fields_api_key_and_optional_team_name():

    assert OCSStrategy.provider_id == "ocs"
    fields = {f["key"]: f for f in OCSStrategy.form_fields}
    assert list(fields) == ["api_key", "team_name"]
    assert fields["api_key"]["editable_on_rotate"] is True
    # team_name is an optional, add-only fallback (auto-detected when possible)
    assert fields["team_name"]["required"] is False
    assert fields["team_name"]["editable_on_rotate"] is False


def test_pack_credential_returns_raw_key():

    assert OCSStrategy.pack_credential({"api_key": "ocs_xxx"}) == "ocs_xxx"


@pytest.mark.asyncio
async def test_verify_and_discover_single_page(httpx_mock, settings):

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/v2/chatbots/?page_size=500",
        json={
            "results": [
                {"id": "exp-1", "name": "Bot One"},
                {"id": "exp-2", "name": "Bot Two"},
            ],
            "next": None,
        },
        status_code=200,
    )
    descriptors = await OCSStrategy.verify_and_discover({"api_key": "k"})
    assert descriptors == [
        TenantDescriptor("exp-1", "Bot One"),
        TenantDescriptor("exp-2", "Bot Two"),
    ]
    request = httpx_mock.get_request()
    assert request.headers["X-api-key"] == "k"


@pytest.mark.asyncio
async def test_verify_and_discover_keeps_each_chatbots_last_activity(httpx_mock, settings, mocker):
    settings.OCS_URL = "https://ocs.example.com"
    checked = datetime(2026, 10, 21, 12, tzinfo=UTC)
    mocker.patch("apps.users.services.tenant_listing.ocs.timezone.now", return_value=checked)
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/v2/chatbots/?page_size=500",
        json={
            "results": [
                {"id": "exp-1", "name": "Live", "last_activity_at": "2026-10-19T14:02:11Z"},
                {"id": "exp-2", "name": "Never used", "last_activity_at": None},
                {"id": "exp-3", "name": "Malformed", "last_activity_at": 42},
            ],
            "next": None,
        },
    )
    descriptors = await OCSStrategy.verify_and_discover({"api_key": "k"})
    assert [d.attributes for d in descriptors] == [
        {"last_activity_at": "2026-10-19T14:02:11Z", "activity_checked_at": checked.isoformat()},
        {"last_activity_at": None, "activity_checked_at": checked.isoformat()},
        None,
    ]


def test_verification_stays_on_the_v1_list():
    request = ocs_listing.list_request("https://ocs.example.com", "api_key", "k")
    assert request.url == "https://ocs.example.com/api/experiments/?page_size=500"


@pytest.mark.asyncio
async def test_verify_and_discover_paginates(httpx_mock, settings):

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/v2/chatbots/?page_size=500",
        json={
            "results": [{"id": "exp-1", "name": "Bot One"}],
            "next": "https://ocs.example.com/api/v2/chatbots/?cursor=xyz",
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/v2/chatbots/?cursor=xyz",
        json={"results": [{"id": "exp-2", "name": "Bot Two"}], "next": None},
    )
    descriptors = await OCSStrategy.verify_and_discover({"api_key": "k"})
    assert [d.external_id for d in descriptors] == ["exp-1", "exp-2"]


@pytest.mark.asyncio
async def test_verify_and_discover_unauthorized(httpx_mock, settings):

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/v2/chatbots/?page_size=500",
        status_code=401,
    )
    with pytest.raises(CredentialVerificationError):
        await OCSStrategy.verify_and_discover({"api_key": "bad"})


@pytest.mark.asyncio
async def test_verify_and_discover_empty_list_raises(httpx_mock, settings):
    """A valid key with no experiments cannot be used as a connection."""

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/v2/chatbots/?page_size=500",
        json={"results": [], "next": None},
        status_code=200,
    )
    with pytest.raises(CredentialVerificationError, match="no experiments"):
        await OCSStrategy.verify_and_discover({"api_key": "k"})


@pytest.mark.asyncio
async def test_verify_for_tenant_passes_when_experiment_present(httpx_mock, settings):

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/experiments/?page_size=500",
        json={"results": [{"id": "exp-1", "name": "Bot"}], "next": None},
    )
    await OCSStrategy.verify_for_tenant({"api_key": "k"}, external_id="exp-1")


@pytest.mark.asyncio
async def test_verify_for_tenant_fails_when_experiment_missing(httpx_mock, settings):

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/experiments/?page_size=500",
        json={"results": [{"id": "exp-other", "name": "Other"}], "next": None},
    )
    with pytest.raises(CredentialVerificationError, match="exp-1"):
        await OCSStrategy.verify_for_tenant({"api_key": "k"}, external_id="exp-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.example.net/api/v2/chatbots/",
        "https://ocs.example.com:8443/api/v2/chatbots/?cursor=xyz",
    ],
)
async def test_verify_and_discover_rejects_foreign_next(httpx_mock, settings, next_url):
    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/v2/chatbots/?page_size=500",
        json={"results": [{"id": "exp-1", "name": "Bot One"}], "next": next_url},
    )
    with pytest.raises(CredentialVerificationError):
        await OCSStrategy.verify_and_discover({"api_key": "secret-key"})
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert all(r.url.host == "ocs.example.com" for r in requests)


@pytest.mark.asyncio
async def test_verify_and_discover_detects_next_cycle(httpx_mock, settings):
    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/v2/chatbots/?page_size=500",
        json={
            "results": [{"id": "exp-1", "name": "Bot One"}],
            "next": "https://ocs.example.com/api/v2/chatbots/?page_size=500",
        },
    )
    with pytest.raises(CredentialVerificationError):
        await OCSStrategy.verify_and_discover({"api_key": "k"})
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_verify_and_discover_follows_relative_next(httpx_mock, settings):
    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/v2/chatbots/?page_size=500",
        json={"results": [{"id": "exp-1", "name": "One"}], "next": "?cursor=xyz"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/v2/chatbots/?cursor=xyz",
        json={"results": [{"id": "exp-2", "name": "Two"}], "next": None},
    )
    descriptors = await OCSStrategy.verify_and_discover({"api_key": "k"})
    assert [d.external_id for d in descriptors] == ["exp-1", "exp-2"]
