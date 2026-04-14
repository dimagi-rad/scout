import pytest


@pytest.mark.asyncio
async def test_valid_credential_returns_domain_info(httpx_mock):
    from apps.users.services.tenant_verification import verify_commcare_credential

    httpx_mock.add_response(
        method="GET",
        url="https://www.commcarehq.org/api/user_domains/v1/",
        json={
            "objects": [
                {"domain_name": "dimagi", "project_name": "Dimagi"},
                {"domain_name": "other", "project_name": "Other"},
            ]
        },
        status_code=200,
    )

    result = await verify_commcare_credential(
        domain="dimagi", username="user@dimagi.org", api_key="secret"
    )

    assert result["domain_name"] == "dimagi"

    request = httpx_mock.get_request()
    assert "/api/user_domains/v1/" in str(request.url)
    assert request.headers["Authorization"] == "ApiKey user@dimagi.org:secret"


@pytest.mark.asyncio
async def test_invalid_credential_raises(httpx_mock):
    from apps.users.services.tenant_verification import (
        CommCareVerificationError,
        verify_commcare_credential,
    )

    httpx_mock.add_response(
        method="GET",
        url="https://www.commcarehq.org/api/user_domains/v1/",
        status_code=401,
    )

    with pytest.raises(CommCareVerificationError):
        await verify_commcare_credential(
            domain="dimagi", username="user@dimagi.org", api_key="wrong"
        )


@pytest.mark.asyncio
async def test_forbidden_raises(httpx_mock):
    from apps.users.services.tenant_verification import (
        CommCareVerificationError,
        verify_commcare_credential,
    )

    httpx_mock.add_response(
        method="GET",
        url="https://www.commcarehq.org/api/user_domains/v1/",
        status_code=403,
    )

    with pytest.raises(CommCareVerificationError):
        await verify_commcare_credential(
            domain="dimagi", username="user@dimagi.org", api_key="secret"
        )


@pytest.mark.asyncio
async def test_server_error_raises(httpx_mock):
    from apps.users.services.tenant_verification import (
        CommCareVerificationError,
        verify_commcare_credential,
    )

    httpx_mock.add_response(
        method="GET",
        url="https://www.commcarehq.org/api/user_domains/v1/",
        status_code=500,
    )

    with pytest.raises(CommCareVerificationError, match="unexpected status 500"):
        await verify_commcare_credential(
            domain="dimagi", username="user@dimagi.org", api_key="secret"
        )


@pytest.mark.asyncio
async def test_wrong_domain_raises(httpx_mock):
    """User is authenticated but doesn't belong to the claimed domain."""
    from apps.users.services.tenant_verification import (
        CommCareVerificationError,
        verify_commcare_credential,
    )

    httpx_mock.add_response(
        method="GET",
        url="https://www.commcarehq.org/api/user_domains/v1/",
        json={"objects": [{"domain_name": "some-other-domain", "project_name": "Other"}]},
        status_code=200,
    )

    with pytest.raises(CommCareVerificationError, match="not a member of domain"):
        await verify_commcare_credential(
            domain="dimagi", username="user@dimagi.org", api_key="secret"
        )


@pytest.mark.asyncio
async def test_no_domains_raises(httpx_mock):
    """User is authenticated but has no domain memberships."""
    from apps.users.services.tenant_verification import (
        CommCareVerificationError,
        verify_commcare_credential,
    )

    httpx_mock.add_response(
        method="GET",
        url="https://www.commcarehq.org/api/user_domains/v1/",
        json={"objects": []},
        status_code=200,
    )

    with pytest.raises(CommCareVerificationError, match="not a member of domain"):
        await verify_commcare_credential(
            domain="dimagi", username="user@dimagi.org", api_key="secret"
        )
