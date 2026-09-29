"""A JSON object body whose fields have the wrong type is a 400, not a 500.

``parse_json_object`` guarantees an object; these cover the views that then
``.strip()``, index or ``.get()`` a field without checking what it holds.
"""

import pytest
from django.test import Client

from apps.chat.views import _last_message_text
from apps.common.http import string_field
from tests.tenant_access import usable_connection

WRONG_TYPES = {"number": 5, "list": ["x"], "object": {"a": 1}, "null": None, "bool": True}


def _post(client, path, body, method="post"):
    return getattr(client, method)(path, data=body, content_type="application/json")


@pytest.fixture
def client(user):
    c = Client()
    c.force_login(user)
    return c


class TestStringField:
    def test_missing_key_gives_default(self):
        assert string_field({}, "email") == ("", None)

    def test_returns_the_string(self):
        assert string_field({"email": "a@b.c"}, "email") == ("a@b.c", None)

    @pytest.mark.parametrize("value", WRONG_TYPES.values(), ids=WRONG_TYPES.keys())
    def test_rejects_non_strings(self, value):
        parsed, err = string_field({"email": value}, "email")

        assert parsed is None
        assert err.status_code == 400


@pytest.mark.django_db
class TestAuthFieldTypes:
    @pytest.mark.parametrize("field", ["email", "password"])
    @pytest.mark.parametrize("value", WRONG_TYPES.values(), ids=WRONG_TYPES.keys())
    def test_email_and_password_must_be_strings(self, field, value):
        body = {"email": "new@example.com", "password": "a-long-passphrase", field: value}

        resp = _post(Client(), "/api/auth/login/", body)

        assert resp.status_code == 400
        assert resp.json()["error"] == f"{field} must be a string."


@pytest.mark.django_db
class TestTenantFieldTypes:
    @pytest.mark.parametrize("value", WRONG_TYPES.values(), ids=WRONG_TYPES.keys())
    def test_select_tenant_id_must_be_a_string(self, client, value):
        resp = _post(client, "/api/auth/tenants/select/", {"tenant_id": value})

        assert resp.status_code == 400
        assert resp.json()["error"] == "tenant_id must be a string."

    def test_select_malformed_tenant_id_is_not_found(self, client):
        resp = _post(client, "/api/auth/tenants/select/", {"tenant_id": "not-a-uuid"})

        assert resp.status_code == 404

    @pytest.mark.parametrize("field", ["provider", "tenant_id"])
    @pytest.mark.parametrize("value", WRONG_TYPES.values(), ids=WRONG_TYPES.keys())
    def test_ensure_fields_must_be_strings(self, client, field, value):
        body = {"provider": "commcare", "tenant_id": "test-domain", field: value}

        resp = _post(client, "/api/auth/tenants/ensure/", body)

        assert resp.status_code == 400
        assert resp.json()["error"] == f"{field} must be a string."


@pytest.mark.django_db
class TestConnectionFieldTypes:
    @pytest.mark.parametrize("value", WRONG_TYPES.values(), ids=WRONG_TYPES.keys())
    def test_add_provider_must_be_a_string(self, client, value):
        resp = _post(client, "/api/auth/connections/", {"provider": value, "fields": {}})

        assert resp.status_code == 400
        assert resp.json()["error"] == "provider must be a string."

    @pytest.mark.parametrize("fields", [5, "x", True], ids=["number", "str", "bool"])
    def test_add_fields_must_be_an_object(self, client, fields):
        resp = _post(client, "/api/auth/connections/", {"provider": "ocs", "fields": fields})

        assert resp.status_code == 400
        assert resp.json()["error"] == "fields must be an object."

    @pytest.mark.parametrize("value", [5, ["x"], {"a": 1}, True])
    @pytest.mark.parametrize("key", ["api_key", "team_name"])
    def test_add_field_values_must_be_strings(self, client, key, value):
        body = {"provider": "ocs", "fields": {"api_key": "k", key: value}}

        resp = _post(client, "/api/auth/connections/", body)

        assert resp.status_code == 400
        assert resp.json()["error"] == f"fields.{key} must be a string."

    @pytest.mark.parametrize(
        ("fields", "error"),
        [
            ("x", "fields must be an object."),
            ({"api_key": 5}, "fields.api_key must be a string."),
            ({"api_key": ["k"]}, "fields.api_key must be a string."),
        ],
    )
    def test_rotate_fields_must_be_strings(self, client, user, fields, error):
        conn = usable_connection(user, "ocs")

        resp = _post(client, f"/api/auth/connections/{conn.id}/", {"fields": fields}, "patch")

        assert resp.status_code == 400
        assert resp.json()["error"] == error


class TestLastMessageText:
    def test_null_content_falls_through_to_parts(self):
        message = {"content": None, "parts": [{"type": "text", "text": "hi"}]}

        assert _last_message_text(message) == ("hi", None)

    def test_ignores_non_text_parts(self):
        message = {"parts": [{"type": "file", "url": 5}, {"type": "text", "text": "hi"}]}

        assert _last_message_text(message) == ("hi", None)


@pytest.mark.django_db
class TestChatFieldTypes:
    @pytest.fixture
    def post(self, client, workspace):
        def post(**body):
            body.setdefault("messages", [{"content": "hi"}])
            body.setdefault("workspaceId", str(workspace.id))
            return _post(client, "/api/chat/", body)

        return post

    @pytest.mark.parametrize("data", [5, ["x"], "x", True], ids=["number", "list", "str", "bool"])
    def test_data_must_be_an_object(self, post, data):
        resp = post(data=data)

        assert resp.status_code == 400
        assert resp.json()["error"] == "data must be an object"

    @pytest.mark.parametrize("messages", [5, {"a": 1}, "hi", True])
    def test_messages_must_be_a_list(self, post, messages):
        resp = post(messages=messages)

        assert resp.status_code == 400
        assert resp.json()["error"] == "messages must be a list"

    @pytest.mark.parametrize(
        ("message", "error"),
        [
            (5, "Each message must be an object"),
            (["hi"], "Each message must be an object"),
            ({"content": 5}, "Message content must be a string"),
            ({"content": ["hi"]}, "Message content must be a string"),
            ({"parts": 5}, "Message parts must be a list of objects"),
            ({"parts": {"type": "text"}}, "Message parts must be a list of objects"),
            ({"parts": ["hi"]}, "Message parts must be a list of objects"),
            ({"parts": [{"type": "text", "text": 5}]}, "Text part text must be a string"),
            ({"parts": [{"type": "text", "text": None}]}, "Text part text must be a string"),
        ],
    )
    def test_malformed_last_message(self, post, message, error):
        resp = post(messages=[message])

        assert resp.status_code == 400
        assert resp.json()["error"] == error

    @pytest.mark.parametrize("value", [5, ["x"], {"a": 1}, True])
    @pytest.mark.parametrize("key", ["workspaceId", "threadId"])
    def test_ids_must_be_strings(self, post, workspace, key, value):
        resp = post(**{key: value})

        assert resp.status_code == 400
        assert resp.json()["error"] == f"{key} must be a string"

    def test_ids_in_data_are_checked_too(self, post):
        resp = post(data={"threadId": ["x"]})

        assert resp.status_code == 400
        assert resp.json()["error"] == "threadId must be a string"
