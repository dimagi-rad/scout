"""Tests for OCSBaseLoader."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mcp_server.loaders.ocs_base import OCSAuthError, OCSBaseLoader


def _make_loader(credential=None, base_url="https://ocs.example"):
    credential = credential or {"type": "oauth", "value": "tok"}
    return OCSBaseLoader(
        experiment_id="exp-uuid-1",
        credential=credential,
        base_url=base_url,
    )


def test_base_loader_sets_auth_header():
    loader = _make_loader()
    assert loader._session.headers["Authorization"] == "Bearer tok"


def test_base_loader_strips_trailing_slash_from_base_url():
    loader = _make_loader(base_url="https://ocs.example/")
    assert loader.base_url == "https://ocs.example"


def test_get_raises_auth_error_on_401():
    loader = _make_loader()
    resp = MagicMock(status_code=401)
    with patch.object(loader._session, "get", return_value=resp), pytest.raises(OCSAuthError):
        loader._get("https://ocs.example/api/experiments/")


def test_paginate_follows_next_cursor():
    loader = _make_loader()
    page1 = MagicMock(status_code=200)
    page1.json.return_value = {
        "results": [{"id": "s1"}, {"id": "s2"}],
        "next": "https://ocs.example/api/sessions/?cursor=abc",
    }
    page2 = MagicMock(status_code=200)
    page2.json.return_value = {"results": [{"id": "s3"}], "next": None}

    with patch.object(loader._session, "get", side_effect=[page1, page2]) as mock_get:
        results = list(loader._paginate("https://ocs.example/api/sessions/"))
        assert results == [[{"id": "s1"}, {"id": "s2"}], [{"id": "s3"}]]
        assert mock_get.call_count == 2


def test_paginate_raises_auth_error_on_403():
    loader = _make_loader()
    resp = MagicMock(status_code=403)
    with patch.object(loader._session, "get", return_value=resp), pytest.raises(OCSAuthError):
        list(loader._paginate("https://ocs.example/api/sessions/"))
