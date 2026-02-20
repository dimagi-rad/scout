from unittest.mock import MagicMock, patch

from mcp_server.loaders.commcare_cases import CommCareCaseLoader


class TestCommCareCaseLoader:
    def test_fetches_and_returns_cases(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "next": None,
            "matching_records": 2,
            "cases": [
                {"case_id": "abc", "case_type": "patient", "properties": {"name": "Alice"}},
                {"case_id": "def", "case_type": "patient", "properties": {"name": "Bob"}},
            ],
        }

        with patch("mcp_server.loaders.commcare_cases.requests.get", return_value=mock_response):
            loader = CommCareCaseLoader(domain="dimagi", access_token="fake-token")
            cases = loader.load()

        assert len(cases) == 2
        assert cases[0]["case_id"] == "abc"

    def test_paginates(self):
        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "next": "https://www.commcarehq.org/a/dimagi/api/case/v2/?cursor=abc",
            "matching_records": 3,
            "cases": [{"case_id": "1"}, {"case_id": "2"}],
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {
            "next": None,
            "matching_records": 3,
            "cases": [{"case_id": "3"}],
        }

        with patch("mcp_server.loaders.commcare_cases.requests.get", side_effect=[page1, page2]):
            loader = CommCareCaseLoader(domain="dimagi", access_token="fake-token")
            cases = loader.load()

        assert len(cases) == 3
