import pytest

from apps.users.services.api_key_providers import CredentialVerificationError


def test_pack_credential_joins_username_and_key():
    from apps.users.services.api_key_providers.commcare import CommCareStrategy

    packed = CommCareStrategy.pack_credential(
        {"domain": "dimagi", "username": "user@d.org", "api_key": "secret"}
    )
    assert packed == "user@d.org:secret"


def test_form_fields_metadata():
    from apps.users.services.api_key_providers.commcare import CommCareStrategy

    keys = [f["key"] for f in CommCareStrategy.form_fields]
    assert keys == ["domain", "username", "api_key"]
    assert CommCareStrategy.provider_id == "commcare"
    by_key = {f["key"]: f for f in CommCareStrategy.form_fields}
    assert by_key["domain"]["editable_on_rotate"] is False
    assert by_key["username"]["editable_on_rotate"] is True
    assert by_key["api_key"]["editable_on_rotate"] is True


@pytest.mark.asyncio
async def test_verify_and_discover_happy_path(httpx_mock):
    from apps.users.services.api_key_providers.commcare import CommCareStrategy

    httpx_mock.add_response(
        method="GET",
        url="https://www.commcarehq.org/api/user_domains/v1/",
        json={"objects": [{"domain_name": "dimagi", "project_name": "Dimagi Inc"}]},
        status_code=200,
    )
    descriptors = await CommCareStrategy.verify_and_discover(
        {"domain": "dimagi", "username": "user@d.org", "api_key": "k"}
    )
    assert descriptors == [("dimagi", "dimagi")]
    request = httpx_mock.get_request()
    assert request.headers["Authorization"] == "ApiKey user@d.org:k"


@pytest.mark.asyncio
async def test_verify_and_discover_unauthorized(httpx_mock):
    from apps.users.services.api_key_providers.commcare import CommCareStrategy

    httpx_mock.add_response(
        method="GET",
        url="https://www.commcarehq.org/api/user_domains/v1/",
        status_code=401,
    )
    with pytest.raises(CredentialVerificationError):
        await CommCareStrategy.verify_and_discover(
            {"domain": "dimagi", "username": "u", "api_key": "k"}
        )


@pytest.mark.asyncio
async def test_verify_and_discover_domain_not_in_list(httpx_mock):
    from apps.users.services.api_key_providers.commcare import CommCareStrategy

    httpx_mock.add_response(
        method="GET",
        url="https://www.commcarehq.org/api/user_domains/v1/",
        json={"objects": [{"domain_name": "other"}]},
        status_code=200,
    )
    with pytest.raises(CredentialVerificationError, match="not a member"):
        await CommCareStrategy.verify_and_discover(
            {"domain": "dimagi", "username": "u", "api_key": "k"}
        )


@pytest.mark.asyncio
async def test_verify_for_tenant_calls_verify_with_external_id(httpx_mock):
    from apps.users.services.api_key_providers.commcare import CommCareStrategy

    httpx_mock.add_response(
        method="GET",
        url="https://www.commcarehq.org/api/user_domains/v1/",
        json={"objects": [{"domain_name": "dimagi"}]},
        status_code=200,
    )
    # Should not raise. Note: form fields on PATCH may omit `domain`; the
    # external_id passed in plays the role of the domain to verify.
    await CommCareStrategy.verify_for_tenant(
        {"username": "u", "api_key": "k"}, external_id="dimagi"
    )
