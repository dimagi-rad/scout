"""Retry + error-shape hardening for CommCare/OCS loaders (arch #252, 12#4/03#6).

Connect already had bounded retry + typed export errors; these tests pin the
matching behaviour on the CommCare and OCS loaders and the CommCare cases
relative-``next`` fix that forms/metadata already had (8774864).
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest
import requests
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.response import HTTPResponse

from apps.common.error_codes import ErrorCode, code_of
from apps.common.errors import ExpectedUpstreamError
from mcp_server.loaders._http import RETRY_TOTAL
from mcp_server.loaders.commcare_base import CommCareExportError, CommCareExportUnavailableError
from mcp_server.loaders.commcare_cases import CommCareCaseLoader
from mcp_server.loaders.commcare_forms import CommCareFormLoader
from mcp_server.loaders.ocs_base import OCSExportError, OCSExportUnavailableError
from mcp_server.loaders.ocs_sessions import OCSSessionLoader

CRED = {"type": "api_key", "value": "u:k"}


def _make_urllib3_response(status, body=b"", headers=None):
    return HTTPResponse(
        body=io.BytesIO(body),
        headers=headers or {},
        status=status,
        version=11,
        version_string="HTTP/1.1",
        reason="",
        preload_content=False,
        decode_content=False,
    )


def _drive_with_statuses(scripted):
    """Patch urllib3's pool so the real retry adapter sees scripted responses."""
    queue = list(scripted)
    calls: list[dict] = []

    def fake_make_request(self, conn, method, url, **kwargs):
        spec = queue.pop(0) if len(queue) > 1 else queue[0]
        status, body, headers = spec
        calls.append({"method": method, "url": url, "status": status})
        return _make_urllib3_response(status, body, headers)

    ctx = patch.multiple(
        HTTPSConnectionPool,
        _make_request=fake_make_request,
        _get_conn=lambda self, timeout=None: MagicMock(),
        _put_conn=lambda self, conn: None,
    )
    return ctx, calls


def _no_backoff(loader):
    adapter = loader._session.get_adapter("https://www.commcarehq.org/")
    adapter.max_retries.backoff_factor = 0
    adapter.max_retries.backoff_jitter = 0
    return loader


def _reset_connection(self, conn, method, url, **kwargs):
    raise ConnectionResetError("reset")


def _reset_connections():
    return patch.multiple(
        HTTPSConnectionPool,
        _make_request=_reset_connection,
        _get_conn=lambda self, timeout=None: MagicMock(),
        _put_conn=lambda self, conn: None,
    )


def _ocs_loader():
    loader = OCSSessionLoader(
        experiment_id="e1", credential={"type": "oauth", "value": "t"}, base_url="https://o.ex"
    )
    adapter = loader._session.get_adapter("https://o.ex/")
    adapter.max_retries.backoff_factor = 0
    adapter.max_retries.backoff_jitter = 0
    return loader


class TestCommCareRetry:
    def test_retries_5xx_then_succeeds(self):
        loader = _no_backoff(CommCareCaseLoader(domain="d", credential=CRED))
        ctx, calls = _drive_with_statuses(
            [
                (500, b"", {}),
                (500, b"", {}),
                (
                    200,
                    b'{"cases": [{"case_id": "c1"}], "matching_records": 1}',
                    {"Content-Type": "application/json"},
                ),
            ]
        )
        with ctx:
            cases = loader.load()
        assert [c["case_id"] for c in cases] == ["c1"]
        assert len(calls) == 3

    def test_raises_export_error_after_exhausting_retries(self):
        loader = _no_backoff(CommCareCaseLoader(domain="d", credential=CRED))
        ctx, calls = _drive_with_statuses([(503, b"", {})])
        with ctx, pytest.raises(CommCareExportUnavailableError) as exc:
            loader.load()
        assert isinstance(exc.value, CommCareExportError)
        assert isinstance(exc.value, ExpectedUpstreamError)
        assert code_of(exc.value) == ErrorCode.UPSTREAM_UNAVAILABLE
        assert len(calls) == RETRY_TOTAL + 1

    def test_404_is_not_retried_or_reclassified(self):
        loader = _no_backoff(CommCareCaseLoader(domain="d", credential=CRED))
        ctx, calls = _drive_with_statuses([(404, b"", {})])
        with ctx, pytest.raises(CommCareExportError) as exc:
            loader.load()
        assert not isinstance(exc.value, ExpectedUpstreamError)
        assert len(calls) == 1

    def test_persistent_connection_error_is_expected(self):
        loader = _no_backoff(CommCareCaseLoader(domain="d", credential=CRED))
        with _reset_connections(), pytest.raises(ExpectedUpstreamError) as exc:
            loader.load()
        assert isinstance(exc.value, CommCareExportError)
        assert code_of(exc.value) == ErrorCode.UPSTREAM_UNAVAILABLE

    @pytest.mark.parametrize(
        "error", [requests.exceptions.SSLError, requests.exceptions.ProxyError]
    )
    def test_tls_and_proxy_failures_stay_reportable(self, error):
        loader = CommCareCaseLoader(domain="d", credential=CRED)
        with (
            patch.object(loader._session, "get", side_effect=error("broken")),
            pytest.raises(error) as exc,
        ):
            loader.load()
        assert not isinstance(exc.value, ExpectedUpstreamError)


class TestCommCareErrorShape:
    def test_cases_missing_key_raises(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"next": None, "matching_records": 0}
        with patch("mcp_server.loaders.commcare_base.requests.Session") as sess_cls:
            session = MagicMock()
            sess_cls.return_value = session
            session.get.return_value = resp
            with pytest.raises(CommCareExportError):
                CommCareCaseLoader(domain="d", credential=CRED).load()

    def test_forms_missing_key_raises(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"meta": {"next": None}}
        with patch("mcp_server.loaders.commcare_base.requests.Session") as sess_cls:
            session = MagicMock()
            sess_cls.return_value = session
            session.get.return_value = resp
            with pytest.raises(CommCareExportError):
                CommCareFormLoader(domain="d", credential=CRED).load()

    def test_invalid_json_raises(self):
        resp = MagicMock(status_code=200)
        resp.json.side_effect = ValueError("boom")
        with patch("mcp_server.loaders.commcare_base.requests.Session") as sess_cls:
            session = MagicMock()
            sess_cls.return_value = session
            session.get.return_value = resp
            with pytest.raises(CommCareExportError):
                CommCareCaseLoader(domain="d", credential=CRED).load()


class TestCommCareCasesRelativeNext:
    def test_relative_next_url_is_resolved(self):
        """Case API v2 may return a bare/relative ``next``; it must be resolved
        against the base URL rather than fed to requests as-is (MissingSchema)."""
        page1 = MagicMock(status_code=200)
        page1.json.return_value = {
            "next": "?cursor=abc",
            "matching_records": 2,
            "cases": [{"case_id": "c1"}],
        }
        page2 = MagicMock(status_code=200)
        page2.json.return_value = {"next": None, "cases": [{"case_id": "c2"}]}
        with patch("mcp_server.loaders.commcare_base.requests.Session") as sess_cls:
            session = MagicMock()
            sess_cls.return_value = session
            session.get.side_effect = [page1, page2]
            cases = CommCareCaseLoader(domain="dimagi", credential=CRED).load()
        assert [c["case_id"] for c in cases] == ["c1", "c2"]
        second_url = session.get.call_args_list[1].args[0]
        assert second_url == "https://www.commcarehq.org/a/dimagi/api/case/v2/?cursor=abc"


class TestOCSErrorShape:
    def test_missing_results_key_raises(self):
        loader = OCSSessionLoader(
            experiment_id="e1", credential={"type": "oauth", "value": "t"}, base_url="https://o.ex"
        )
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"next": None}
        with patch.object(loader._session, "get", return_value=resp):
            with pytest.raises(OCSExportError):
                list(loader.load_pages())


class TestOCSRetry:
    def test_retries_502_then_succeeds(self):
        loader = _ocs_loader()
        ctx, calls = _drive_with_statuses(
            [
                (502, b"", {}),
                (
                    200,
                    b'{"results": [{"id": "s1"}], "next": null}',
                    {"Content-Type": "application/json"},
                ),
            ]
        )
        with ctx:
            rows = loader.load()
        assert len(rows) == 1
        assert len(calls) == 2

    def test_repeated_502_raises_unavailable_after_all_attempts(self):
        loader = _ocs_loader()
        ctx, calls = _drive_with_statuses([(502, b"", {})])
        with ctx, pytest.raises(OCSExportUnavailableError) as exc:
            loader.load()
        assert isinstance(exc.value, OCSExportError)
        assert isinstance(exc.value, ExpectedUpstreamError)
        assert code_of(exc.value) == ErrorCode.UPSTREAM_UNAVAILABLE
        assert len(calls) == RETRY_TOTAL + 1

    def test_404_is_not_retried_or_reclassified(self):
        loader = _ocs_loader()
        ctx, calls = _drive_with_statuses([(404, b"", {})])
        with ctx, pytest.raises(OCSExportError) as exc:
            loader.load()
        assert not isinstance(exc.value, ExpectedUpstreamError)
        assert len(calls) == 1

    def test_persistent_connection_error_is_expected(self):
        loader = _ocs_loader()
        with _reset_connections(), pytest.raises(ExpectedUpstreamError) as exc:
            loader.load()
        assert isinstance(exc.value, OCSExportError)
        assert code_of(exc.value) == ErrorCode.UPSTREAM_UNAVAILABLE

    @pytest.mark.parametrize(
        "error", [requests.exceptions.SSLError, requests.exceptions.ProxyError]
    )
    def test_tls_and_proxy_failures_stay_reportable(self, error):
        loader = _ocs_loader()
        with (
            patch.object(loader._session, "get", side_effect=error("broken")),
            pytest.raises(error) as exc,
        ):
            loader.load()
        assert not isinstance(exc.value, ExpectedUpstreamError)
