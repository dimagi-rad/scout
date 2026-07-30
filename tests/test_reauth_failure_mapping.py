"""Reauth guidance mapping in the failure summary (arch #252, finding 14#4)."""

from __future__ import annotations

from types import SimpleNamespace

from apps.workspaces.tasks import _compose_failure_summary, _looks_like_auth_failure


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
