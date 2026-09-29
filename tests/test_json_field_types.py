"""A JSON object body whose fields have the wrong type is a 400, not a 500.

``parse_json_object`` guarantees an object; these cover the views that then
``.strip()``, index or ``.get()`` a field without checking what it holds.
"""

import pytest
from django.test import Client

from apps.common.http import string_field

WRONG_TYPES = {"number": 5, "list": ["x"], "object": {"a": 1}, "null": None, "bool": True}


def _post(client, path, body, method="post"):
    return getattr(client, method)(path, data=body, content_type="application/json")


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
    @pytest.mark.parametrize("path", ["/api/auth/login/", "/api/auth/signup/"])
    @pytest.mark.parametrize("field", ["email", "password"])
    @pytest.mark.parametrize("value", WRONG_TYPES.values(), ids=WRONG_TYPES.keys())
    def test_email_and_password_must_be_strings(self, path, field, value):
        body = {"email": "new@example.com", "password": "a-long-passphrase", field: value}

        resp = _post(Client(), path, body)

        assert resp.status_code == 400
        assert resp.json()["error"] == f"{field} must be a string."
