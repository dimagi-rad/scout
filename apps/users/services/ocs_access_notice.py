"""Tell the user when OCS refused their chatbot list at sign-in.

On a first connect of a team no ``TenantConnection`` exists yet, so the denial
cannot be recorded on one and Connected Accounts shows nothing; the user lands
on an empty onboarding screen. The session carries the refused teams to the SPA
via ``/api/auth/me/``, keyed by team so one team's later success clears only its
own refusal, until the user dismisses them.
"""

from __future__ import annotations

from apps.users.services.oauth_scope import account_scope
from apps.users.services.ocs_team_flow import teams_from_claims

SESSION_KEY = "ocs_access_denied"


def _team(account) -> dict:
    slug = account_scope(account)
    names = {t["slug"]: t["name"] for t in teams_from_claims(account.extra_data) or []}
    return {"slug": slug, "name": names.get(slug, slug)}


def with_refusal(refused: dict | None, account) -> dict:
    team = _team(account)
    return {**(refused or {}), team["slug"]: team}


def without_refusal(refused: dict | None, account) -> dict:
    return {k: v for k, v in (refused or {}).items() if k != account_scope(account)}


def payload(refused: dict | None) -> dict | None:
    """The ``/me`` shape; a team-less identity's refusal has an empty slug."""
    if not refused:
        return None
    return {"teams": sorted(refused.values(), key=lambda t: t["slug"])}


def record_refusal(request, account) -> None:
    if request is not None and hasattr(request, "session"):
        request.session[SESSION_KEY] = with_refusal(request.session.get(SESSION_KEY), account)


def clear_refusal(request, account) -> None:
    if request is not None and hasattr(request, "session") and SESSION_KEY in request.session:
        remaining = without_refusal(request.session[SESSION_KEY], account)
        if remaining:
            request.session[SESSION_KEY] = remaining
        else:
            del request.session[SESSION_KEY]
