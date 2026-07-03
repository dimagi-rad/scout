"""Mid-run 401 -> refresh -> retry across loaders (arch #252, finding 14#3).

A CommCare OAuth token lives ~15 min; a large sync exceeds that and 401s
mid-load. The loaders consult a ``refresh`` callable on the credential to mint
a fresh token and retry the failing page, so the run outlives the token.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mcp_server.loaders.commcare_cases import CommCareCaseLoader
from mcp_server.loaders.connect_visits import ConnectVisitLoader
from mcp_server.loaders.ocs_sessions import OCSSessionLoader


def _resp(status_code, json_body=None):
    r = MagicMock(status_code=status_code)
    r.json.return_value = json_body or {}
    return r


class TestCommCareMidRunRefresh:
    def test_401_midrun_refreshes_and_continues(self):
        refresh = MagicMock(return_value="fresh-token")
        credential = {"type": "oauth", "value": "stale-token", "refresh": refresh}

        page1 = _resp(
            200, {"next": "?cursor=2", "matching_records": 2, "cases": [{"case_id": "1"}]}
        )
        unauthorized = _resp(401)
        page2 = _resp(200, {"next": None, "cases": [{"case_id": "2"}]})

        with patch("mcp_server.loaders.commcare_base.requests.Session") as sess_cls:
            session = MagicMock()
            session.headers = {}
            sess_cls.return_value = session
            # Page 1 ok, then the token expires (401), refresh, retry -> page 2.
            session.get.side_effect = [page1, unauthorized, page2]
            loader = CommCareCaseLoader(domain="d", credential=credential)
            cases = loader.load()

        assert [c["case_id"] for c in cases] == ["1", "2"]
        refresh.assert_called_once()
        assert session.headers["Authorization"] == "Bearer fresh-token"

    def test_no_refresh_callable_still_raises_auth_error(self):
        from mcp_server.loaders.commcare_base import CommCareAuthError

        credential = {"type": "api_key", "value": "u:k"}  # no refresh
        with patch("mcp_server.loaders.commcare_base.requests.Session") as sess_cls:
            session = MagicMock()
            session.headers = {}
            sess_cls.return_value = session
            session.get.return_value = _resp(401)
            loader = CommCareCaseLoader(domain="d", credential=credential)
            try:
                loader.load()
                raised = False
            except CommCareAuthError:
                raised = True
        assert raised


class TestConnectMidRunRefresh:
    def test_401_on_export_page_refreshes_and_retries(self):
        refresh = MagicMock(return_value="fresh-token")
        credential = {"type": "oauth", "value": "stale", "refresh": refresh}
        loader = ConnectVisitLoader(
            opportunity_id=1, credential=credential, base_url="https://c.example"
        )
        unauthorized = _resp(401)
        page = _resp(200, {"next": None, "results": [{"id": 1}], "count": 1})
        with patch.object(loader._session, "get", side_effect=[unauthorized, page]):
            rows = [r for pg, _ in loader.load_pages() for r in pg]
        assert [r["visit_id"] for r in rows] == [1]
        refresh.assert_called_once()


class TestOCSMidRunRefresh:
    def test_401_on_paginate_refreshes_and_retries(self):
        refresh = MagicMock(return_value="fresh-token")
        credential = {"type": "oauth", "value": "stale", "refresh": refresh}
        loader = OCSSessionLoader(
            experiment_id="e1", credential=credential, base_url="https://o.example"
        )
        unauthorized = _resp(401)
        page = _resp(200, {"results": [{"id": "s1"}], "next": None})
        with patch.object(loader._session, "get", side_effect=[unauthorized, page]):
            rows = [r for pg, _ in loader.load_pages() for r in pg]
        assert [r["session_id"] for r in rows] == ["s1"]
        refresh.assert_called_once()
