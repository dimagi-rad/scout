from unittest.mock import MagicMock

import pytest


def _make_app_response():
    return {
        "objects": [
            {
                "id": "app_abc",
                "name": "CHW App",
                "modules": [
                    {
                        "name": "Patient Registration",
                        "case_type": "patient",
                        "forms": [
                            {
                                "xmlns": "http://openrosa.org/formdesigner/form1",
                                "name": "Patient Registration",
                                "questions": [
                                    {
                                        "label": "Patient Name",
                                        "tag": "input",
                                        "value": "/data/name",
                                    },
                                    {"label": "Age", "tag": "input", "value": "/data/age"},
                                ],
                            }
                        ],
                    },
                    {
                        "name": "Household Visit",
                        "case_type": "household",
                        "forms": [],
                    },
                ],
            }
        ],
        "meta": {"next": None},
    }


class TestCommCareMetadataLoader:
    def _mock_session(self, responses):
        """Return a patch context that intercepts Session().get() calls."""
        import unittest.mock as mock

        session = MagicMock()
        if isinstance(responses, list):
            session.get.side_effect = responses
        else:
            session.get.return_value = responses
        return mock.patch("mcp_server.loaders.commcare_base.requests.Session", return_value=session)

    def test_loads_app_definitions(self):
        from mcp_server.loaders.commcare_metadata import CommCareMetadataLoader

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_app_response()

        with self._mock_session(mock_resp):
            loader = CommCareMetadataLoader(
                domain="dimagi", credential={"type": "api_key", "value": "user:key"}
            )
            result = loader.load()

        assert result["app_definitions"][0]["id"] == "app_abc"
        assert result["app_definitions"][0]["name"] == "CHW App"

    def test_extracts_unique_case_types(self):
        from mcp_server.loaders.commcare_metadata import CommCareMetadataLoader

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_app_response()

        with self._mock_session(mock_resp):
            loader = CommCareMetadataLoader(
                domain="dimagi", credential={"type": "api_key", "value": "user:key"}
            )
            result = loader.load()

        case_type_names = [ct["name"] for ct in result["case_types"]]
        assert "patient" in case_type_names
        assert "household" in case_type_names
        assert len(case_type_names) == len(set(case_type_names))

    def test_extracts_form_definitions(self):
        from mcp_server.loaders.commcare_metadata import CommCareMetadataLoader

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_app_response()

        with self._mock_session(mock_resp):
            loader = CommCareMetadataLoader(
                domain="dimagi", credential={"type": "api_key", "value": "user:key"}
            )
            result = loader.load()

        form_defs = result["form_definitions"]
        assert "http://openrosa.org/formdesigner/form1" in form_defs
        assert form_defs["http://openrosa.org/formdesigner/form1"]["case_type"] == "patient"

    def test_raises_on_auth_failure(self):
        from mcp_server.loaders.commcare_base import CommCareAuthError
        from mcp_server.loaders.commcare_metadata import CommCareMetadataLoader

        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with self._mock_session(mock_resp), pytest.raises(CommCareAuthError):
            CommCareMetadataLoader(
                domain="dimagi", credential={"type": "api_key", "value": "bad"}
            ).load()

    def test_paginates_apps(self):
        """Pagination must follow ``meta.next`` (TastyPie envelope), not a
        top-level ``next`` field. A top-level ``next`` is intentionally
        included to ensure it is IGNORED.
        """
        from mcp_server.loaders.commcare_metadata import CommCareMetadataLoader

        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "objects": [{"id": "app1", "name": "App 1", "modules": []}],
            # Top-level next must be ignored.
            "next": None,
            "meta": {"next": "/a/dimagi/api/v0.5/application/?offset=1&limit=100"},
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {
            "objects": [{"id": "app2", "name": "App 2", "modules": []}],
            "meta": {"next": None},
        }

        with self._mock_session([page1, page2]) as mock_session_cls:
            loader = CommCareMetadataLoader(
                domain="dimagi", credential={"type": "api_key", "value": "user:key"}
            )
            result = loader.load()

        assert len(result["app_definitions"]) == 2
        session = mock_session_cls.return_value
        assert session.get.call_count == 2
        second_call_url = session.get.call_args_list[1].args[0]
        assert second_call_url == (
            "https://www.commcarehq.org/a/dimagi/api/v0.5/application/?offset=1&limit=100"
        )

    def test_resolves_query_string_only_meta_next(self):
        """Regression test: when CommCare returns ``meta.next`` as a bare
        query string (e.g. ``?limit=100&offset=100``), ``urljoin`` must
        resolve it against the base URL. The prior ``startswith("/")``
        shim passed these through unresolved and caused ``MissingSchema``.
        """
        from mcp_server.loaders.commcare_metadata import CommCareMetadataLoader

        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "objects": [{"id": "app1", "name": "App 1", "modules": []}],
            "meta": {"next": "?limit=100&offset=100"},
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {
            "objects": [{"id": "app2", "name": "App 2", "modules": []}],
            "meta": {"next": None},
        }

        with self._mock_session([page1, page2]) as mock_session_cls:
            loader = CommCareMetadataLoader(
                domain="dimagi", credential={"type": "api_key", "value": "user:key"}
            )
            result = loader.load()

        assert len(result["app_definitions"]) == 2
        session = mock_session_cls.return_value
        assert session.get.call_count == 2
        second_call_url = session.get.call_args_list[1].args[0]
        assert second_call_url == (
            "https://www.commcarehq.org/a/dimagi/api/v0.5/application/?limit=100&offset=100"
        )
