"""A request body that isn't a JSON object is a 400, not a 500.

``json.loads`` accepts arrays, strings and numbers, and the views then call
``.get()`` on the result. ``parse_json_object`` is the one gate they share.
"""

import uuid

import pytest
from django.test import Client, RequestFactory

from apps.common.http import parse_json_object

BAD_BODIES = {
    "array": "[1, 2]",
    "string": '"hello"',
    "number": "42",
    "invalid": "{not json",
    "non_utf8": b"\xff\xfe",
    "empty": "",
}


def _post(body):
    return RequestFactory().post("/", data=body, content_type="application/json")


class TestParseJsonObject:
    @pytest.mark.parametrize("body", BAD_BODIES.values(), ids=BAD_BODIES.keys())
    def test_rejects_non_objects(self, body):
        parsed, err = parse_json_object(_post(body))

        assert parsed is None
        assert err.status_code == 400

    def test_returns_the_object(self):
        parsed, err = parse_json_object(_post('{"a": 1}'))

        assert (parsed, err) == ({"a": 1}, None)

    def test_allow_empty_treats_empty_body_as_empty_object(self):
        assert parse_json_object(_post(""), allow_empty=True) == ({}, None)
        assert parse_json_object(_post(" \n"), allow_empty=True) == ({}, None)

    def test_allow_empty_still_rejects_a_non_object(self):
        _parsed, err = parse_json_object(_post("[]"), allow_empty=True)

        assert err.status_code == 400

    def test_deeply_nested_json_is_a_400(self):
        _parsed, err = parse_json_object(_post("[" * 100_000))

        assert err.status_code == 400


@pytest.mark.django_db
class TestEndpointsRejectNonObjectBodies:
    @pytest.mark.parametrize("body", BAD_BODIES.values(), ids=BAD_BODIES.keys())
    def test_login(self, body):
        resp = Client().post("/api/auth/login/", data=body, content_type="application/json")

        assert resp.status_code == 400

    @pytest.mark.parametrize(
        "path",
        ["/api/chat/", "/api/auth/tenants/select/", "/api/auth/tenants/ensure/"],
    )
    @pytest.mark.parametrize("body", BAD_BODIES.values(), ids=BAD_BODIES.keys())
    def test_authenticated_endpoints(self, user, path, body):
        client = Client()
        client.force_login(user)

        resp = client.post(path, data=body, content_type="application/json")

        assert resp.status_code == 400

    @pytest.mark.parametrize("body", BAD_BODIES.values(), ids=BAD_BODIES.keys())
    def test_thread_title_patch(self, user, workspace, body):
        client = Client()
        client.force_login(user)

        resp = client.patch(
            f"/api/workspaces/{workspace.id}/threads/{uuid.uuid4()}/",
            data=body,
            content_type="application/json",
        )

        assert resp.status_code == 400
