import pytest

from apps.users.services.api_key_providers import CredentialVerificationError


def test_form_fields_only_api_key():
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    assert OCSStrategy.provider_id == "ocs"
    keys = [f["key"] for f in OCSStrategy.form_fields]
    assert keys == ["api_key"]
    assert OCSStrategy.form_fields[0]["editable_on_rotate"] is True


def test_pack_credential_returns_raw_key():
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    assert OCSStrategy.pack_credential({"api_key": "ocs_xxx"}) == "ocs_xxx"


@pytest.mark.asyncio
async def test_verify_and_discover_single_page(httpx_mock, settings):
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/experiments/",
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
    assert descriptors == [("exp-1", "Bot One"), ("exp-2", "Bot Two")]
    request = httpx_mock.get_request()
    assert request.headers["X-api-key"] == "k"


@pytest.mark.asyncio
async def test_verify_and_discover_paginates(httpx_mock, settings):
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/experiments/",
        json={
            "results": [{"id": "exp-1", "name": "Bot One"}],
            "next": "https://ocs.example.com/api/experiments/?cursor=xyz",
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/experiments/?cursor=xyz",
        json={"results": [{"id": "exp-2", "name": "Bot Two"}], "next": None},
    )
    descriptors = await OCSStrategy.verify_and_discover({"api_key": "k"})
    assert [d.external_id for d in descriptors] == ["exp-1", "exp-2"]


@pytest.mark.asyncio
async def test_verify_and_discover_unauthorized(httpx_mock, settings):
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/experiments/",
        status_code=401,
    )
    with pytest.raises(CredentialVerificationError):
        await OCSStrategy.verify_and_discover({"api_key": "bad"})


@pytest.mark.asyncio
async def test_verify_and_discover_empty_list_raises(httpx_mock, settings):
    """A valid key with no experiments cannot be used as a connection."""
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/experiments/",
        json={"results": [], "next": None},
        status_code=200,
    )
    with pytest.raises(CredentialVerificationError, match="no experiments"):
        await OCSStrategy.verify_and_discover({"api_key": "k"})


@pytest.mark.asyncio
async def test_verify_for_tenant_passes_when_experiment_present(httpx_mock, settings):
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/experiments/",
        json={"results": [{"id": "exp-1", "name": "Bot"}], "next": None},
    )
    await OCSStrategy.verify_for_tenant({"api_key": "k"}, external_id="exp-1")


@pytest.mark.asyncio
async def test_verify_for_tenant_fails_when_experiment_missing(httpx_mock, settings):
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/experiments/",
        json={"results": [{"id": "exp-other", "name": "Other"}], "next": None},
    )
    with pytest.raises(CredentialVerificationError, match="exp-1"):
        await OCSStrategy.verify_for_tenant({"api_key": "k"}, external_id="exp-1")
