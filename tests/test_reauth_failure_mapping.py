"""Reauth guidance mapping in the failure summary (arch #252, finding 14#4)."""

from __future__ import annotations

from types import SimpleNamespace

from apps.workspaces.tasks import (
    _compose_failure_summary,
    _looks_like_access_denied,
    _looks_like_auth_failure,
)


def _run(sources: dict, state: str = "failed"):
    return SimpleNamespace(result={"sources": sources}, state=state)


def test_looks_like_auth_failure_detects_markers():
    assert _looks_like_auth_failure(
        "CommCareAuthError: CommCare authentication failed ... reconnect your CommCare account"
    )
    assert _looks_like_auth_failure("OCSAuthError: HTTP 401")
    assert not _looks_like_auth_failure("ConnectExportError: HTTP 500 for ...")
    assert not _looks_like_auth_failure(None)


def test_summary_appends_reauth_guidance_on_auth_failure():
    runs = [
        _run(
            {
                "cases": {
                    "state": "failed",
                    "rows": 0,
                    "error": (
                        "CommCareAuthError: CommCare authentication failed for domain d "
                        "(HTTP 401). Please reconnect your CommCare account and retry."
                    ),
                }
            }
        )
    ]
    summary = _compose_failure_summary(runs)
    assert "reconnect the affected account" in summary.lower()


def test_summary_omits_reauth_guidance_for_non_auth_failure():
    runs = [
        _run(
            {
                "visits": {
                    "state": "failed",
                    "rows": 0,
                    "error": "ConnectExportError: HTTP 500 for /export/...",
                }
            }
        )
    ]
    summary = _compose_failure_summary(runs)
    assert "reconnect" not in summary.lower()


_DENIED_ERROR = (
    "OCSAccessDeniedError: Your Open Chat Studio account no longer has access to "
    "chatbot 514e2e67 (HTTP 403). Your sign-in is still valid, so reconnecting "
    "will not help."
)
_EXPIRED_ERROR = (
    "OCSTokenExpiredError: OCS authentication failed for experiment cdec8c04 "
    "(HTTP 401). Your Open Chat Studio sign-in has expired or been revoked — "
    "please reconnect your account and retry."
)


def test_403_is_not_classified_as_a_reauthable_failure():
    assert _looks_like_access_denied(_DENIED_ERROR)
    assert not _looks_like_auth_failure(_DENIED_ERROR)


def test_401_is_classified_as_reauthable_and_not_access_denied():
    assert _looks_like_auth_failure(_EXPIRED_ERROR)
    assert not _looks_like_access_denied(_EXPIRED_ERROR)


def test_summary_tells_the_user_reconnecting_will_not_fix_a_403():
    summary = _compose_failure_summary(
        [_run({"sessions": {"state": "failed", "rows": 0, "error": _DENIED_ERROR}})]
    )
    assert "will not restore it" in summary.lower()
    # The old behaviour — the loop this issue is about.
    assert "reconnect the affected account" not in summary.lower()


def test_summary_carries_both_when_two_sources_fail_differently():
    """One tenant's token can be dead while another's access was revoked."""
    summary = _compose_failure_summary(
        [
            _run(
                {
                    "sessions": {"state": "failed", "rows": 0, "error": _DENIED_ERROR},
                    "messages": {"state": "failed", "rows": 0, "error": _EXPIRED_ERROR},
                }
            )
        ]
    )
    assert "reconnect the affected account" in summary.lower()
    assert "will not restore it" in summary.lower()
