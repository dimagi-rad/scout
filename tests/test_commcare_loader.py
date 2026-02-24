from unittest.mock import MagicMock, patch


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

        with patch("mcp_server.loaders.commcare_base.requests.Session") as mock_session_cls:
            session = MagicMock()
            mock_session_cls.return_value = session
            session.get.return_value = mock_response
            from mcp_server.loaders.commcare_cases import CommCareCaseLoader

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

        with patch("mcp_server.loaders.commcare_base.requests.Session") as mock_session_cls:
            session = MagicMock()
            mock_session_cls.return_value = session
            session.get.side_effect = [page1, page2]
            from mcp_server.loaders.commcare_cases import CommCareCaseLoader

            loader = CommCareCaseLoader(domain="dimagi", access_token="fake-token")
            cases = loader.load()

        assert len(cases) == 3


class TestCommCareBaseLoader:
    def test_build_auth_header_api_key(self):
        from mcp_server.loaders.commcare_base import build_auth_header

        h = build_auth_header({"type": "api_key", "value": "user@example.com:abc"})
        assert h["Authorization"] == "ApiKey user@example.com:abc"

    def test_build_auth_header_oauth(self):
        from mcp_server.loaders.commcare_base import build_auth_header

        h = build_auth_header({"type": "oauth", "value": "tok123"})
        assert h["Authorization"] == "Bearer tok123"

    def test_http_timeout_is_tuple(self):
        from mcp_server.loaders.commcare_base import HTTP_TIMEOUT

        assert isinstance(HTTP_TIMEOUT, tuple)
        assert len(HTTP_TIMEOUT) == 2


class TestCaseLoaderLoadPages:
    def test_load_pages_yields_pages(self):
        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "next": "https://www.commcarehq.org/a/dimagi/api/case/v2/?cursor=x",
            "cases": [{"case_id": "c1"}, {"case_id": "c2"}],
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {"next": None, "cases": [{"case_id": "c3"}]}

        with patch("mcp_server.loaders.commcare_base.requests.Session") as mock_session_cls:
            session = MagicMock()
            mock_session_cls.return_value = session
            session.get.side_effect = [page1, page2]

            from mcp_server.loaders.commcare_cases import CommCareCaseLoader

            loader = CommCareCaseLoader(
                domain="dimagi", credential={"type": "api_key", "value": "u:k"}
            )
            pages = list(loader.load_pages())

        assert len(pages) == 2
        assert len(pages[0]) == 2
        assert len(pages[1]) == 1

    def test_load_is_flat_list(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "next": None,
            "cases": [{"case_id": "c1"}, {"case_id": "c2"}],
        }

        with patch("mcp_server.loaders.commcare_base.requests.Session") as mock_session_cls:
            session = MagicMock()
            mock_session_cls.return_value = session
            session.get.return_value = mock_resp

            from mcp_server.loaders.commcare_cases import CommCareCaseLoader

            loader = CommCareCaseLoader(
                domain="dimagi", credential={"type": "api_key", "value": "u:k"}
            )
            cases = loader.load()

        assert len(cases) == 2
