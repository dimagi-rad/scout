"""Verify PROVIDER_TOKEN_URLS match actual OAuth adapter endpoints."""

from apps.users.auth_views import PROVIDER_TOKEN_URLS
from apps.users.providers.commcare.views import CommCareOAuth2Adapter
from apps.users.providers.commcare_connect.views import CommCareConnectOAuth2Adapter


class TestProviderTokenUrls:
    def test_commcare_token_url_matches_adapter(self):
        adapter_url = CommCareOAuth2Adapter.access_token_url
        refresh_url = PROVIDER_TOKEN_URLS["commcare"]
        assert adapter_url == refresh_url, (
            f"CommCare token URL mismatch: Adapter={adapter_url}, Refresh={refresh_url}"
        )

    def test_connect_token_url_matches_adapter(self):
        adapter_url = CommCareConnectOAuth2Adapter.access_token_url
        refresh_url = PROVIDER_TOKEN_URLS["commcare_connect"]
        assert adapter_url == refresh_url, (
            f"Connect token URL mismatch: Adapter={adapter_url}, Refresh={refresh_url}"
        )
