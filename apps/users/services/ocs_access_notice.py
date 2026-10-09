"""Tell the user when OCS refused their chatbot list at sign-in.

On a first connect of a team no ``TenantConnection`` exists yet, so the denial
cannot be recorded on one and Connected Accounts shows nothing; the user lands
on an empty onboarding screen. The session carries the notice to the SPA via
``/api/auth/me/`` until the user dismisses it or a later OCS sign-in succeeds.
"""

from __future__ import annotations

from apps.users.services.oauth_scope import account_scope
from apps.users.services.ocs_team_flow import teams_from_claims

SESSION_KEY = "ocs_access_denied"


def notice_for(account) -> dict:
    slug = account_scope(account)
    names = {t["slug"]: t["name"] for t in teams_from_claims(account.extra_data) or []}
    return {"team": {"slug": slug, "name": names.get(slug, slug)} if slug else None}


def set_notice(request, account) -> None:
    if request is not None and hasattr(request, "session"):
        request.session[SESSION_KEY] = notice_for(account)


def clear_notice(request) -> None:
    if request is not None and hasattr(request, "session"):
        request.session.pop(SESSION_KEY, None)
