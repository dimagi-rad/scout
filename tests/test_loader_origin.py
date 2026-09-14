"""Requests must never send provider credentials outside the configured origin."""

from unittest.mock import Mock
from urllib.parse import urljoin, urlsplit

import pytest
import requests_mock

from mcp_server.loaders.commcare_cases import CommCareCaseLoader
from mcp_server.loaders.connect_base import ConnectBaseLoader
from mcp_server.loaders.ocs_base import OCSBaseLoader


@pytest.fixture(
    params=["commcare_oauth", "commcare_api_key", "connect_oauth", "ocs_oauth", "ocs_api_key"]
)
def provider(request):
    kind, auth = request.param.split("_", 1)
    refresh = Mock(return_value="fresh")
    credential = {"type": auth, "value": "secret", "refresh": refresh}
    if kind == "commcare":
        loader = CommCareCaseLoader(domain="test", credential=credential)
        url = "https://www.commcarehq.org/a/test/api/case/v2/"
        consume = loader.load

        def payload(next_url):
            return {"cases": [], "next": next_url}
    elif kind == "connect":
        loader = ConnectBaseLoader(1, credential, base_url="https://provider.example")
        url = loader._opp_url("records/")

        def consume():
            return list(loader._paginate_export_pages("records/"))

        def payload(next_url):
            return {"results": [], "next": next_url}
    else:
        loader = OCSBaseLoader("exp", credential, base_url="https://provider.example")
        url = "https://provider.example/api/records/"

        def consume():
            return list(loader._paginate(url))

        def payload(next_url):
            return {"results": [], "next": next_url}

    return loader, url, consume, payload, refresh


@pytest.mark.parametrize("next_url", ["https://foreign.example/page", "//foreign.example/page"])
def test_foreign_next_never_requested(provider, next_url):
    _loader, url, consume, payload, refresh = provider
    with requests_mock.Mocker() as mock:
        mock.get(url, json=payload(next_url))
        mock.get("https://foreign.example/page", status_code=401)
        with pytest.raises(ValueError, match="origin"):
            consume()
        assert len(mock.request_history) == 1
        assert mock.last_request.url.split("?")[0] == url
        refresh.assert_not_called()


@pytest.mark.parametrize("location", ["https://foreign.example/page", "//foreign.example/page"])
def test_foreign_redirect_never_requested(provider, location):
    _loader, url, consume, _payload, refresh = provider
    with requests_mock.Mocker() as mock:
        mock.get(url, status_code=302, headers={"Location": location})
        mock.get("https://foreign.example/page", status_code=401)
        with pytest.raises(ValueError, match="origin"):
            consume()
        assert len(mock.request_history) == 1
        assert mock.last_request.url.split("?")[0] == url
        refresh.assert_not_called()


@pytest.mark.parametrize("next_url", ["?cursor=2", "/second/?cursor=2"])
def test_relative_next(provider, next_url):
    _loader, url, consume, payload, _refresh = provider
    target = urljoin(url, next_url)
    with requests_mock.Mocker() as mock:
        mock.get(url, json=payload(next_url))
        mock.get(target, json=payload(None))
        consume()
        assert len(mock.request_history) == 2
        assert mock.last_request.url == target


def test_plaintext_next_upgraded_before_request(provider):
    _loader, url, consume, payload, _refresh = provider
    target = url + "?cursor=2"
    with requests_mock.Mocker() as mock:
        mock.get(url, json=payload(target.replace("https:", "http:")))
        mock.get(target, json=payload(None))
        consume()
        assert len(mock.request_history) == 2
        assert mock.request_history[0].url.split("?")[0] == url
        assert mock.last_request.url == target


@pytest.mark.parametrize(
    "target",
    [
        "https://foreign.example/page?token=must-not-appear",
        "https://user:must-not-appear@provider.example/page",
        "https://provider.example:444/page",
        "https://provider.example:0/page",
        "http://provider.example:8080/page",
        "https://provider.example:bad/page",
        "file:///etc/passwd",
        "https://[broken/page",
        "https://provider.example\\@foreign.example/page",
        "https://provider.example\n.foreign.example/page",
    ],
)
def test_invalid_request_target_is_rejected_without_echoing_url(target):
    loader = OCSBaseLoader(
        "exp", {"type": "api_key", "value": "secret"}, "https://provider.example"
    )
    with requests_mock.Mocker() as mock:
        with pytest.raises(ValueError) as caught:
            loader._get(target)
        assert "must-not-appear" not in str(caught.value)
        assert mock.request_history == []


@pytest.mark.parametrize(
    "base",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
        "https://provider.example:8443",
        "https://PROVIDER.example:443",
    ],
)
def test_explicit_configured_origins_work(base):
    loader = OCSBaseLoader("exp", {"type": "api_key", "value": "secret"}, base)
    with requests_mock.Mocker() as mock:
        mock.get(base + "/page", json={})
        loader._get(base + "/page")
        assert mock.last_request.headers["X-api-key"] == "secret"


@pytest.mark.parametrize(
    "base,target",
    [
        ("http://provider.example", "http://provider.example/page"),
        ("http://localhost:8000", "http://localhost:8001/page"),
        ("http://localhost:8000", "http://127.0.0.1:8000/page"),
        ("https://provider.example:8443", "http://provider.example:8443/page"),
        ("https://provider.example", "http://localhost/page"),
    ],
)
def test_http_exception_and_custom_ports_are_origin_scoped(base, target):
    loader = OCSBaseLoader("exp", {"type": "api_key", "value": "secret"}, base)
    with requests_mock.Mocker() as mock:
        with pytest.raises(ValueError):
            loader._get(target)
        assert mock.request_history == []


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_safe_redirect_then_foreign_redirect_blocks_second_hop(provider, status):
    _loader, url, consume, _payload, refresh = provider
    second = url + "second/"
    with requests_mock.Mocker() as mock:
        mock.get(url, status_code=status, headers={"Location": "second/"})
        mock.get(second, status_code=status, headers={"Location": "https://foreign.example/page"})
        mock.get("https://foreign.example/page", status_code=401)
        with pytest.raises(ValueError, match="origin"):
            consume()
        assert len(mock.request_history) == 2
        assert mock.last_request.url == second
        refresh.assert_not_called()


def test_redirect_cycle_is_bounded(provider):
    loader, url, consume, _payload, refresh = provider
    loader._session.max_redirects = 2
    with requests_mock.Mocker() as mock:
        mock.get(url, status_code=302, headers={"Location": url})
        with pytest.raises(ValueError, match="redirect limit"):
            consume()
        assert len(mock.request_history) == 3
        refresh.assert_not_called()


def test_redirect_refresh_preserves_headers_and_drops_original_params():
    refresh = Mock(return_value="fresh")
    loader = ConnectBaseLoader(
        1, {"value": "stale", "refresh": refresh}, "https://provider.example"
    )
    url = loader._opp_url("records/")
    second = "https://provider.example/moved/?cursor=2"
    with requests_mock.Mocker() as mock:
        mock.get(url, status_code=302, headers={"Location": "/moved/?cursor=2"})
        mock.get(
            second,
            [
                {"status_code": 401},
                {"json": {"results": [], "next": None}},
            ],
        )
        list(loader._paginate_export_pages("records/", params={"filter": "one"}))
        assert [r.url for r in mock.request_history] == [url + "?filter=one", second, second]
        assert [r.headers["Authorization"] for r in mock.request_history] == [
            "Bearer stale",
            "Bearer stale",
            "Bearer fresh",
        ]
        assert all(
            r.headers["Accept"] == "application/json; version=2.0" for r in mock.request_history
        )
        refresh.assert_called_once()


def test_refresh_budget_does_not_reset_after_redirect():
    refresh = Mock(return_value="fresh")
    loader = OCSBaseLoader(
        "exp", {"value": "stale", "refresh": refresh}, "https://provider.example"
    )
    with requests_mock.Mocker() as mock:
        mock.get(
            "https://provider.example/page",
            [
                {"status_code": 401},
                {"status_code": 302, "headers": {"Location": "/second"}},
            ],
        )
        mock.get("https://provider.example/second", status_code=401)
        with pytest.raises(Exception, match="401"):
            loader._get("https://provider.example/page")
        assert len(mock.request_history) == 3
        refresh.assert_called_once()


def test_next_resolves_against_redirected_page(provider):
    _loader, url, consume, payload, _refresh = provider
    parts = urlsplit(url)
    moved = f"{parts.scheme}://{parts.netloc}/moved/page/"
    with requests_mock.Mocker() as mock:
        mock.get(url, status_code=302, headers={"Location": moved})
        mock.get(moved, json=payload("next/"))
        mock.get(moved + "next/", json=payload(None))
        consume()
        assert mock.last_request.url == moved + "next/"


def test_malformed_redirect_does_not_echo_provider_url():
    loader = OCSBaseLoader(
        "exp", {"type": "api_key", "value": "secret"}, "https://provider.example"
    )
    with requests_mock.Mocker() as mock:
        mock.get(
            "https://provider.example/page",
            status_code=302,
            headers={
                "Location": "https://provider.example:bad/page?token=must-not-appear",
            },
        )
        with pytest.raises(ValueError) as caught:
            loader._get("https://provider.example/page")
        assert "must-not-appear" not in str(caught.value)
        assert len(mock.request_history) == 1


def test_plaintext_redirect_is_upgraded_before_request(provider):
    _loader, url, consume, payload, _refresh = provider
    target = url + "second/"
    with requests_mock.Mocker() as mock:
        mock.get(url, status_code=302, headers={"Location": target.replace("https:", "http:")})
        mock.get(target, json=payload(None))
        consume()
        assert len(mock.request_history) == 2
        assert mock.last_request.url == target
        assert all(r.url.startswith("https:") for r in mock.request_history)


def test_invalid_page_error_does_not_echo_cursor_url(provider):
    _loader, url, consume, payload, _refresh = provider
    target = url + "?token=must-not-appear"
    with requests_mock.Mocker() as mock:
        mock.get(url, json=payload(target))
        mock.get(target, json={})
        with pytest.raises(Exception, match="missing") as caught:
            consume()
        assert "must-not-appear" not in str(caught.value)


def test_connect_metadata_error_does_not_echo_cursor_url():
    loader = ConnectBaseLoader(1, {"value": "secret"}, "https://provider.example")
    with requests_mock.Mocker() as mock:
        mock.get("https://provider.example/page?token=must-not-appear", status_code=400)
        with pytest.raises(Exception, match="400") as caught:
            loader._get("https://provider.example/page?token=must-not-appear")
        assert "must-not-appear" not in str(caught.value)
