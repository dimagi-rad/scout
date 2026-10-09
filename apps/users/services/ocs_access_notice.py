"""Tell the user when OCS refused their chatbot list at sign-in.

On a first connect of a team no ``TenantConnection`` exists yet, so the denial
cannot be recorded on one and Connected Accounts shows nothing; the user lands
on an empty onboarding screen. The session carries the refused teams to the SPA
via ``/api/auth/me/``, keyed by team. A team's refusal clears when the user
dismisses it or the team later resolves: on its next sign-in, or on ``/me``'s
re-resolve, which runs only before onboarding completes.
"""

from __future__ import annotations

from apps.users.services.oauth_scope import account_scope, ocs_scope_unusable
from apps.users.services.ocs_team_flow import teams_from_claims


def session_key(user_pk) -> str:
    # Written before allauth logs the user in, so an abandoned sign-in would leave it on
    # the anonymous session for whoever logs in next on that browser; the pk scopes it.
    return f"ocs_access_denied:{user_pk}"


def _team(account) -> dict:
    slug = account_scope(account)
    names = {t["slug"]: t["name"] for t in teams_from_claims(account.extra_data) or []}
    return {"slug": slug, "name": names.get(slug, slug)}


def with_refusal(refused: dict | None, account) -> dict:
    team = _team(account)
    return {**(refused or {}), team["slug"]: team}


def without_refusal(refused: dict | None, account) -> dict:
    """``refused`` less ``account``'s team, whose resolve has just returned.

    An unusable scope returns [] without asking OCS, so it proves nothing about access.
    """
    scope = account_scope(account)
    if ocs_scope_unusable("ocs", scope):
        return dict(refused or {})
    return {k: v for k, v in (refused or {}).items() if k != scope}


def payload(refused: dict | None) -> dict | None:
    """The ``/me`` shape; a team-less identity's refusal has an empty slug."""
    if not refused:
        return None
    return {"teams": sorted(refused.values(), key=lambda t: t["slug"])}


def record_refusal(request, user, account) -> None:
    if request is not None and hasattr(request, "session"):
        key = session_key(user.pk)
        request.session[key] = with_refusal(request.session.get(key), account)


def clear_refusal(request, user, account) -> None:
    key = session_key(user.pk)
    if request is not None and hasattr(request, "session") and key in request.session:
        remaining = without_refusal(request.session[key], account)
        if remaining:
            request.session[key] = remaining
        else:
            del request.session[key]
