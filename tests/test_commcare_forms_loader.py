from unittest.mock import MagicMock

import pytest


def _mock_session(responses):
    import unittest.mock as mock

    session = MagicMock()
    if isinstance(responses, list):
        session.get.side_effect = responses
    else:
        session.get.return_value = responses
    return mock.patch("mcp_server.loaders.commcare_base.requests.Session", return_value=session)


class TestCommCareFormLoader:
    def test_fetches_forms(self):
        from mcp_server.loaders.commcare_forms import CommCareFormLoader

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "next": None,
            "meta": {"total_count": 2},
            "objects": [
                {
                    "id": "f1",
                    "form": {"@name": "Reg", "case": {"@case_id": "c1", "@action": "create"}},
                    "received_on": "2026-01-01",
                },
                {"id": "f2", "form": {"@name": "Follow"}, "received_on": "2026-01-02"},
            ],
        }

        with _mock_session(mock_resp):
            loader = CommCareFormLoader(
                domain="dimagi", credential={"type": "api_key", "value": "user:key"}
            )
            forms = loader.load()

        assert len(forms) == 2
        assert forms[0]["form_id"] == "f1"

    def test_paginates(self):
        from mcp_server.loaders.commcare_forms import CommCareFormLoader

        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "next": "https://www.commcarehq.org/a/dimagi/api/v0.5/form/?cursor=x",
            "meta": {"total_count": 3},
            "objects": [{"id": "f1", "form": {}}, {"id": "f2", "form": {}}],
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {
            "next": None,
            "meta": {"total_count": 3},
            "objects": [{"id": "f3", "form": {}}],
        }

        with _mock_session([page1, page2]):
            forms = CommCareFormLoader(
                domain="dimagi", credential={"type": "api_key", "value": "user:key"}
            ).load()

        assert len(forms) == 3

    def test_load_pages_yields_per_page(self):
        from mcp_server.loaders.commcare_forms import CommCareFormLoader

        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "next": "https://www.commcarehq.org/a/dimagi/api/v0.5/form/?cursor=x",
            "objects": [{"id": "f1", "form": {}}, {"id": "f2", "form": {}}],
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {"next": None, "objects": [{"id": "f3", "form": {}}]}

        with _mock_session([page1, page2]):
            pages = list(
                CommCareFormLoader(
                    domain="dimagi", credential={"type": "api_key", "value": "user:key"}
                ).load_pages()
            )

        assert len(pages) == 2
        assert len(pages[0]) == 2
        assert len(pages[1]) == 1

    def test_raises_on_auth_failure(self):
        from mcp_server.loaders.commcare_base import CommCareAuthError
        from mcp_server.loaders.commcare_forms import CommCareFormLoader

        mock_resp = MagicMock()
        mock_resp.status_code = 403

        with _mock_session(mock_resp):
            with pytest.raises(CommCareAuthError):
                CommCareFormLoader(
                    domain="dimagi", credential={"type": "api_key", "value": "bad"}
                ).load()


class TestExtractCaseRefs:
    """Tests for the nested case-reference extractor."""

    def test_extracts_top_level_case(self):
        from mcp_server.loaders.commcare_forms import extract_case_refs

        form_data = {"case": {"@case_id": "abc", "@action": "create", "update": {"name": "Alice"}}}
        refs = extract_case_refs(form_data)
        assert len(refs) == 1
        assert refs[0]["case_id"] == "abc"
        assert refs[0]["action"] == "create"

    def test_extracts_nested_case(self):
        from mcp_server.loaders.commcare_forms import extract_case_refs

        form_data = {
            "name": "Alice",
            "child_group": {"case": {"@case_id": "child1", "@action": "update"}},
        }
        refs = extract_case_refs(form_data)
        assert len(refs) == 1
        assert refs[0]["case_id"] == "child1"

    def test_extracts_multiple_cases_from_repeat_group(self):
        from mcp_server.loaders.commcare_forms import extract_case_refs

        form_data = {
            "repeat_item": [
                {"case": {"@case_id": "r1", "@action": "create"}},
                {"case": {"@case_id": "r2", "@action": "create"}},
            ]
        }
        refs = extract_case_refs(form_data)
        assert len(refs) == 2
        assert {r["case_id"] for r in refs} == {"r1", "r2"}

    def test_ignores_non_case_dicts(self):
        from mcp_server.loaders.commcare_forms import extract_case_refs

        form_data = {"name": "test", "age": 30, "meta": {"timeEnd": "2026-01-01"}}
        assert extract_case_refs(form_data) == []

    def test_deduplicates_same_case_id(self):
        from mcp_server.loaders.commcare_forms import extract_case_refs

        form_data = {
            "case": {"@case_id": "same", "@action": "create"},
            "group": {"case": {"@case_id": "same", "@action": "update"}},
        }
        refs = extract_case_refs(form_data)
        assert [r["case_id"] for r in refs].count("same") == 1
