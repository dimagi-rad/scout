"""401 and 403 must not be conflated in any loader (#372).

A 401 means the credential is dead and reconnecting mints a working one. A 403
means the credential is *fine* and simply has no access to that resource, so
reconnecting mints an identically-scoped token and fails the same way — the
advice loops the user. Every loader raise site is pinned here, in both
directions, by the ``ErrorCode`` it carries.

Also pinned: no loader message contains remediation advice at all. That is the
presentation layer's job, stated once and keyed by code — see rule 3 in
``apps/common/errors.py``.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from apps.common.error_codes import ErrorCode
from apps.common.errors import (
    CommCareAccessDeniedError,
    CommCareTokenExpiredError,
    ConnectAccessDeniedError,
    ConnectTokenExpiredError,
    ExpectedUpstreamError,
    OCSAccessDeniedError,
    OCSTokenExpiredError,
)
from mcp_server.loaders.commcare_base import CommCareBaseLoader
from mcp_server.loaders.connect_base import ConnectBaseLoader
from mcp_server.loaders.ocs_base import OCSBaseLoader

CREDENTIAL = {"type": "oauth", "value": "tok"}


def _response(status: int) -> Mock:
    resp = Mock()
    resp.status_code = status
    resp.ok = status < 400
    resp.headers = {}
    return resp


def _ocs():
    return OCSBaseLoader("exp-1", CREDENTIAL, base_url="https://ocs.test")


def _commcare():
    return CommCareBaseLoader("dom-1", CREDENTIAL)


def _connect():
    return ConnectBaseLoader(765, CREDENTIAL, base_url="https://connect.test")


# (label, loader factory, call, 401 class, 403 class, resource name in the copy)
CASES = [
    (
        "ocs._get",
        _ocs,
        lambda ldr: ldr._get("https://ocs.test/api/experiments/exp-1/"),
        OCSTokenExpiredError,
        OCSAccessDeniedError,
        "exp-1",
    ),
    (
        "commcare._get",
        _commcare,
        lambda ldr: ldr._get("https://hq.test/a/dom-1/api/case/v2/"),
        CommCareTokenExpiredError,
        CommCareAccessDeniedError,
        "dom-1",
    ),
    (
        "connect._get",
        _connect,
        lambda ldr: ldr._get("https://connect.test/export/opportunity/765/meta/"),
        ConnectTokenExpiredError,
        ConnectAccessDeniedError,
        "765",
    ),
    (
        "connect._paginate_export_pages",
        _connect,
        lambda ldr: list(ldr._paginate_export_pages("visits")),
        ConnectTokenExpiredError,
        ConnectAccessDeniedError,
        "765",
    ),
]
IDS = [c[0] for c in CASES]


@pytest.mark.parametrize(
    ("_label", "factory", "call", "expired_cls", "_denied", "_res"), CASES, ids=IDS
)
def test_401_raises_token_expired(_label, factory, call, expired_cls, _denied, _res):
    loader = factory()
    with patch.object(loader._session, "get", return_value=_response(401)):
        with pytest.raises(expired_cls) as exc:
            call(loader)
    assert exc.value.code == ErrorCode.AUTH_TOKEN_EXPIRED
    assert "401" in str(exc.value)


@pytest.mark.parametrize(
    ("_label", "factory", "call", "_expired", "denied_cls", "resource"), CASES, ids=IDS
)
def test_403_raises_access_denied(_label, factory, call, _expired, denied_cls, resource):
    loader = factory()
    with patch.object(loader._session, "get", return_value=_response(403)):
        with pytest.raises(denied_cls) as exc:
            call(loader)
    message = str(exc.value)
    assert exc.value.code == ErrorCode.AUTH_ACCESS_DENIED
    assert resource in message, "the 403 must name the resource the user lost access to"
    assert "still valid" in message.lower(), "a 403 must say the credential itself is fine"


# Remediation phrasings a loader must never contain. "reconnect" in any form is
# the one that mattered for #372 — a 403 told the user to reconnect and they
# looped — but the rule is broader than that one bug: what the user should DO is
# the presentation layer's to say, once, keyed by code (rule 3 in
# apps/common/errors.py). While both layers wrote advice the user got it twice in
# two different phrasings.
_ADVICE_PHRASES = (
    "reconnect",
    "re-connect",
    "please ",
    "retry",
    "ask a",
    "ask an",
    "remove this data source",
    "contact ",
)


@pytest.mark.parametrize("status", [401, 403], ids=["401", "403"])
@pytest.mark.parametrize(
    ("_label", "factory", "call", "_expired", "_denied", "_res"), CASES, ids=IDS
)
def test_loader_messages_describe_and_never_advise(
    _label, factory, call, _expired, _denied, _res, status
):
    loader = factory()
    with patch.object(loader._session, "get", return_value=_response(status)):
        with pytest.raises(ExpectedUpstreamError) as exc:
            call(loader)
    message = str(exc.value).lower()
    found = [phrase for phrase in _ADVICE_PHRASES if phrase in message]
    assert not found, f"loader message gives remediation advice ({found}): {message}"


@pytest.mark.parametrize(
    ("_label", "factory", "call", "expired_cls", "denied_cls", "_res"), CASES, ids=IDS
)
def test_403_is_not_refresh_retried(_label, factory, call, expired_cls, denied_cls, _res):
    """_http.get_with_auth_refresh consults the refresher only on 401.

    Pinned here because the transport already drew this distinction and the
    loaders were the layer discarding it.
    """
    refresher = Mock(return_value="new-token")
    loader = factory()
    loader._refresh = refresher
    with patch.object(loader._session, "get", return_value=_response(403)):
        with pytest.raises(denied_cls):
            call(loader)
    refresher.assert_not_called()
