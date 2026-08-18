"""CommCare Connect OAuth follows the selected deployment environment."""

from urllib.parse import parse_qs, urlparse

import pytest
from allauth.socialaccount.models import SocialApp
from django.contrib.sites.models import Site
from django.test import override_settings


@pytest.mark.django_db
@override_settings(CONNECT_API_URL="https://connect-staging.dimagi.com")
def test_connect_login_redirects_to_staging(client):
    site, _ = Site.objects.update_or_create(
        id=1,
        defaults={"domain": "testserver", "name": "Test Server"},
    )
    app = SocialApp.objects.create(
        provider="commcare_connect",
        name="CommCare Connect",
        client_id="staging-client-id",
        secret="staging-client-secret",
    )
    app.sites.add(site)

    response = client.post("/accounts/commcare_connect/login/?next=/")

    assert response.status_code == 302
    location = response.headers["Location"]
    parsed = urlparse(location)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://connect-staging.dimagi.com/o/authorize/"
    )
    params = parse_qs(parsed.query)
    assert params["client_id"] == ["staging-client-id"]
    assert params["redirect_uri"] == ["http://testserver/accounts/commcare_connect/login/callback/"]
