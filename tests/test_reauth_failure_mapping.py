"""Credential guidance in the failure summary (arch #252, finding 14#4; #372).

Guidance is selected by ``error_code`` — the machine half of the per-source
failure record — never by matching the human message. These tests deliberately
pair a code with a *contradictory* message to pin that: if a future change goes
back to reading the prose, they fail.
"""

from __future__ import annotations

from types import SimpleNamespace

from apps.common.error_codes import ErrorCode
from apps.workspaces.tasks import _compose_failure_summary, _credential_guidance, _SourceFailure

_DENIED_ERROR = (
    "OCSAccessDeniedError: Your Open Chat Studio account no longer has access to "
    "chatbot 514e2e67 (HTTP 403)."
)
_EXPIRED_ERROR = (
    "OCSTokenExpiredError: OCS authentication failed for experiment cdec8c04 (HTTP 401)."
)


def _failed(error: str, code: str) -> dict:
    return {"state": "failed", "rows": 0, "error": error, "error_code": code}


def _run(sources: dict, state: str = "failed"):
    return SimpleNamespace(result={"sources": sources}, state=state)


def test_guidance_is_keyed_on_the_code_not_the_message():
    """A message saying "401" with a 403 code gets 403 advice, and vice versa.

    The whole point of the code: prose is not evidence.
    """
    lines = _credential_guidance(
        [
            _SourceFailure(
                "sessions", "HTTP 401 reconnect your account", ErrorCode.AUTH_ACCESS_DENIED
            ),
            _SourceFailure("messages", "HTTP 403 access removed", ErrorCode.AUTH_TOKEN_EXPIRED),
        ]
    )
    assert "sessions: access was removed upstream" in " ".join(lines)
    assert "messages: expired or revoked sign-in" in " ".join(lines)


def test_no_guidance_without_a_credential_code():
    assert (
        _credential_guidance([_SourceFailure("visits", "HTTP 500", ErrorCode.INTERNAL_ERROR)]) == []
    )


def test_guidance_groups_every_source_sharing_a_code():
    lines = _credential_guidance(
        [
            _SourceFailure("sessions", "", ErrorCode.AUTH_TOKEN_EXPIRED),
            _SourceFailure("messages", "", ErrorCode.AUTH_TOKEN_EXPIRED),
        ]
    )
    assert len(lines) == 1
    assert lines[0].startswith("sessions, messages: ")


def test_summary_appends_reauth_guidance_on_a_401():
    summary = _compose_failure_summary(
        [_run({"cases": _failed(_EXPIRED_ERROR, ErrorCode.AUTH_TOKEN_EXPIRED)})]
    )
    assert "reconnect the affected account" in summary.lower()


def test_summary_omits_reauth_guidance_for_a_non_credential_failure():
    summary = _compose_failure_summary(
        [
            _run(
                {
                    "visits": _failed(
                        "ConnectExportError: HTTP 500 for /export/...",
                        ErrorCode.INTERNAL_ERROR,
                    )
                }
            )
        ]
    )
    assert "reconnect" not in summary.lower()


def test_summary_tells_the_user_reconnecting_will_not_fix_a_403():
    summary = _compose_failure_summary(
        [_run({"sessions": _failed(_DENIED_ERROR, ErrorCode.AUTH_ACCESS_DENIED)})]
    )
    assert "will not restore it" in summary.lower()
    # The loop #372 is about: a 403 must never draw reconnect advice.
    assert "reconnect the affected account" not in summary.lower()


def test_summary_attributes_each_kind_of_advice_to_its_own_source():
    """A dead token on one source and revoked access on another.

    The two need opposite advice, so both must name their source — unattributed,
    "reconnect" followed by "reconnecting will NOT restore it" reads as a
    contradiction the user cannot act on (#372).
    """
    summary = _compose_failure_summary(
        [
            _run(
                {
                    "sessions": _failed(_DENIED_ERROR, ErrorCode.AUTH_ACCESS_DENIED),
                    "messages": _failed(_EXPIRED_ERROR, ErrorCode.AUTH_TOKEN_EXPIRED),
                }
            )
        ]
    )
    assert "messages: expired or revoked sign-in" in summary
    assert "sessions: access was removed upstream" in summary


def test_summary_survives_a_failure_record_with_no_code():
    """An in-flight run written by the previous release carries no error_code.

    It degrades to "no credential guidance", never to a crash or to wrong advice.
    """
    summary = _compose_failure_summary(
        [_run({"cases": {"state": "failed", "rows": 0, "error": _EXPIRED_ERROR}})]
    )
    assert _EXPIRED_ERROR in summary
    assert "reconnect" not in summary.lower()
