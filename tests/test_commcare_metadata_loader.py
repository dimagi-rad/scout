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
        "next": None,
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

        with self._mock_session(mock_resp):
            with pytest.raises(CommCareAuthError):
                CommCareMetadataLoader(
                    domain="dimagi", credential={"type": "api_key", "value": "bad"}
                ).load()

    def test_paginates_apps(self):
        from mcp_server.loaders.commcare_metadata import CommCareMetadataLoader

        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "objects": [{"id": "app1", "name": "App 1", "modules": []}],
            "next": "https://www.commcarehq.org/a/dimagi/api/v0.5/application/?offset=1",
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {
            "objects": [{"id": "app2", "name": "App 2", "modules": []}],
            "next": None,
        }

        with self._mock_session([page1, page2]):
            loader = CommCareMetadataLoader(
                domain="dimagi", credential={"type": "api_key", "value": "user:key"}
            )
            result = loader.load()

        assert len(result["app_definitions"]) == 2
