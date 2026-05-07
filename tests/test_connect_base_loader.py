import pytest
import requests_mock as rm

from mcp_server.loaders.connect_base import (
    EXPORT_ACCEPT_HEADER,
    ConnectAuthError,
    ConnectBaseLoader,
    ConnectExportError,
)


@pytest.fixture
def loader():
    return ConnectBaseLoader(
        opportunity_id=814,
        credential={"type": "oauth", "value": "test-token-123"},
        base_url="https://connect.example.com",
    )


class TestConnectBaseLoader:
    def test_get_json(self, loader):
        with rm.Mocker() as m:
            m.get(
                "https://connect.example.com/export/opportunity/814/",
                json={"id": 814, "name": "Test Opp"},
                status_code=200,
            )
            resp = loader._get("https://connect.example.com/export/opportunity/814/")
            assert resp.json()["id"] == 814

    def test_auth_error_on_401(self, loader):
        with rm.Mocker() as m:
            m.get(
                "https://connect.example.com/export/opportunity/814/",
                status_code=401,
            )
            with pytest.raises(ConnectAuthError):
                loader._get("https://connect.example.com/export/opportunity/814/")

    def test_auth_error_on_403(self, loader):
        with rm.Mocker() as m:
            m.get(
                "https://connect.example.com/export/opportunity/814/",
                status_code=403,
            )
            with pytest.raises(ConnectAuthError):
                loader._get("https://connect.example.com/export/opportunity/814/")

    def test_bearer_token_header(self, loader):
        assert loader._session.headers["Authorization"] == "Bearer test-token-123"

    def test_get_does_not_send_versioned_accept_header(self, loader):
        """Plain ``_get`` is used by ConnectMetadataLoader for non-versioned
        endpoints; it must not advertise version=2.0."""
        with rm.Mocker() as m:
            m.get(
                "https://connect.example.com/export/opp_org_program_list/",
                json={"organizations": []},
            )
            loader._get("https://connect.example.com/export/opp_org_program_list/")
            accept = m.last_request.headers.get("Accept", "")
            assert "version=2.0" not in accept


class TestPaginateExportPages:
    def test_single_page(self, loader):
        with rm.Mocker() as m:
            m.get(
                "https://connect.example.com/export/opportunity/814/user_visits/",
                json={
                    "next": None,
                    "previous": None,
                    "results": [{"id": 1}, {"id": 2}],
                    "count": 2,
                },
            )
            pages = list(loader._paginate_export_pages("user_visits/"))
            assert len(pages) == 1
            assert pages[0] == ([{"id": 1}, {"id": 2}], 2)

    def test_multi_page_follows_next(self, loader):
        first = "https://connect.example.com/export/opportunity/814/user_visits/"
        second = f"{first}?last_id=1"
        third = f"{first}?last_id=2"
        with rm.Mocker() as m:
            m.get(
                first,
                json={"next": second, "previous": None, "results": [{"id": 1}], "count": 3},
            )
            m.get(second, json={"next": third, "previous": None, "results": [{"id": 2}]})
            m.get(third, json={"next": None, "previous": None, "results": [{"id": 3}]})

            pages = list(loader._paginate_export_pages("user_visits/"))
            all_records = [r for page, _ in pages for r in page]
            assert [r["id"] for r in all_records] == [1, 2, 3]
            # count is exposed only on the first page.
            assert pages[0][1] == 3
            assert pages[1][1] is None
            assert pages[2][1] is None

    def test_sends_versioned_accept_header(self, loader):
        with rm.Mocker() as m:
            m.get(
                "https://connect.example.com/export/opportunity/814/user_visits/",
                json={"next": None, "previous": None, "results": []},
            )
            list(loader._paginate_export_pages("user_visits/"))
            assert m.last_request.headers["Accept"] == EXPORT_ACCEPT_HEADER

    def test_initial_params_passed_only_on_first_request(self, loader):
        """Subsequent requests follow ``next`` verbatim and must not append
        the original params (the server already preserved them in ``next``)."""
        first = "https://connect.example.com/export/opportunity/814/user_visits/"
        second = f"{first}?last_id=1&page_size=2&order=asc"
        with rm.Mocker() as m:
            m.get(first, json={"next": second, "previous": None, "results": [{"id": 1}]})
            m.get(second, json={"next": None, "previous": None, "results": [{"id": 2}]})

            list(loader._paginate_export_pages("user_visits/", params={"images": "true"}))

            # First request must include images=true.
            assert m.request_history[0].qs.get("images") == ["true"]
            # Second request must NOT have images appended again — it should
            # follow `next` exactly and only carry the server-preserved params.
            assert "images" not in m.request_history[1].qs

    def test_auth_error_on_401(self, loader):
        with rm.Mocker() as m:
            m.get(
                "https://connect.example.com/export/opportunity/814/user_visits/",
                status_code=401,
            )
            with pytest.raises(ConnectAuthError):
                list(loader._paginate_export_pages("user_visits/"))

    def test_export_error_on_invalid_json(self, loader):
        with rm.Mocker() as m:
            m.get(
                "https://connect.example.com/export/opportunity/814/user_visits/",
                text="not json",
                headers={"Content-Type": "application/json"},
            )
            with pytest.raises(ConnectExportError):
                list(loader._paginate_export_pages("user_visits/"))

    def test_export_error_on_missing_results_key(self, loader):
        with rm.Mocker() as m:
            m.get(
                "https://connect.example.com/export/opportunity/814/user_visits/",
                json={"next": None},
            )
            with pytest.raises(ConnectExportError):
                list(loader._paginate_export_pages("user_visits/"))

    def test_follows_http_to_https_redirect_on_next_url(self, loader):
        """Regression test for dimagi/commcare-connect#1109.

        The production CommCare Connect server has been observed to return
        ``next`` URLs with the ``http://`` scheme even when the original
        request came in over HTTPS. This is a server-side bug (gunicorn
        strips ``X-Forwarded-Proto: https`` because ``--forwarded-allow-ips``
        defaults to 127.0.0.1, so Django's ``request.build_absolute_uri()``
        falls back to ``http``).

        The mitigation: scout uses ``requests`` whose ``allow_redirects``
        default is ``True``, so following the upstream 301 → HTTPS happens
        for free, and ``Session.should_strip_auth`` has a special case for
        same-host HTTP→HTTPS upgrades on default ports that preserves the
        ``Authorization`` header. This test pins both behaviors so a future
        regression can't silently re-introduce the bug.
        """
        first_https = "https://connect.example.com/export/opportunity/814/user_visits/"
        # Server returns http://... in `next` (the bug).
        next_http = "http://connect.example.com/export/opportunity/814/user_visits/?last_id=1"
        next_https = "https://connect.example.com/export/opportunity/814/user_visits/?last_id=1"

        with rm.Mocker() as m:
            m.get(first_https, json={"next": next_http, "results": [{"id": 1}]})
            # Edge layer 301-redirects http -> https.
            m.get(next_http, status_code=301, headers={"Location": next_https})
            m.get(next_https, json={"next": None, "results": [{"id": 2}]})

            all_records = [
                r for page, _ in loader._paginate_export_pages("user_visits/") for r in page
            ]
            assert [r["id"] for r in all_records] == [1, 2]

            # The redirected request must still carry the bearer token —
            # requests preserves Authorization on same-host http→https upgrades.
            redirected_request = next(req for req in m.request_history if req.url == next_https)
            assert redirected_request.headers["Authorization"] == "Bearer test-token-123"
