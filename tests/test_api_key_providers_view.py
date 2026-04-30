import pytest


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(email="u@example.com", password="pw")


def test_returns_strategy_metadata(client, user):
    client.force_login(user)
    resp = client.get("/api/auth/api-key-providers/")
    assert resp.status_code == 200
    providers = resp.json()
    by_id = {p["id"]: p for p in providers}
    assert "commcare" in by_id
    assert "ocs" in by_id
    assert by_id["commcare"]["display_name"] == "CommCare HQ"
    assert by_id["ocs"]["display_name"] == "Open Chat Studio"
    ocs_field_keys = [f["key"] for f in by_id["ocs"]["fields"]]
    assert ocs_field_keys == ["api_key"]
    cc_field_keys = [f["key"] for f in by_id["commcare"]["fields"]]
    assert cc_field_keys == ["domain", "username", "api_key"]


def test_unauthenticated_returns_401(client, db):
    resp = client.get("/api/auth/api-key-providers/")
    assert resp.status_code == 401
