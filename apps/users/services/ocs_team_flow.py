"""Connect OCS teams one at a time, or all of them in a chain, pinned by slug.

OCS emits a ``teams`` claim (scope ``teams``) listing every team the user belongs
to, and its authorize view accepts ``?team=<slug>`` to pin the token's team. If the
slug is not one of the user's teams OCS silently falls back to the session team, so
the callback compares the returned ``team`` claim with the slug Scout asked for
(carried in allauth's per-flow OAuth state, not the session) and refuses a mismatch.

Progress lives in the session under :data:`SESSION_KEY`. Each hop is still a
CSRF-protected POST to allauth's login view started by the SPA; the session only
records which team is in flight, so a hop that never comes back (closed tab, OCS
error page) stops the chain instead of looping on it.
"""

from __future__ import annotations

import re
import time
from typing import Any

SESSION_KEY = "ocs_team_flow"

# Key in allauth's per-flow OAuth state naming the team Scout pinned the flow to.
REQUESTED_TEAM_STATE_KEY = "ocs_requested_team"

# A chain idle this long between hops (tab closed mid-pause) must not resume by
# itself on the user's next visit, possibly days later.
CHAIN_IDLE_SECONDS = 10 * 60

# A hop still on OCS (or a read from another tab mid-hop) is not yet "incomplete".
PENDING_GRACE_SECONDS = 20

MODE_ALL = "all"
MODE_ONE = "one"

STOP_MISMATCH = "mismatch"
STOP_CANCELLED = "cancelled"
STOP_FAILED = "failed"
STOP_INCOMPLETE = "incomplete"
STOP_USER = "user"
STOP_IDLE = "idle"

# OCS team slugs are Django slugs; anything else is not forwarded to OCS.
_SLUG_RE = re.compile(r"^[-a-zA-Z0-9_]{1,100}$")


def valid_slug(value: Any) -> str:
    """``value`` as a team slug, or "" if it is not one."""
    slug = str(value or "").strip()
    return slug if _SLUG_RE.match(slug) else ""


def teams_from_claims(data: dict | None) -> list[dict] | None:
    """The sanitized ``teams`` claim, or None when the response carried none.

    None (no scope granted, or an OCS that predates the claim) differs from []:
    the UI can only say a team is unconnected when it knows the full list.
    """
    raw = (data or {}).get("teams")
    if not isinstance(raw, list):
        return None
    teams = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        slug = valid_slug(item.get("slug"))
        if slug and slug not in teams:
            teams[slug] = {"slug": slug, "name": str(item.get("name") or slug)[:200]}
    return sorted(teams.values(), key=lambda t: t["slug"])


def subject_of(account) -> str:
    return str((account.extra_data or {}).get("sub") or account.uid.partition("#")[0])


def known_teams(accounts) -> list[dict] | None:
    """Every team the user's OCS identities report, or None if none reports a list.

    Each OCS user (subject) contributes the list from its most recently used
    identity, so a team left since an older connect drops out.
    """
    latest: dict[str, tuple] = {}
    for account in accounts:
        teams = teams_from_claims(account.extra_data)
        if teams is None:
            continue
        sub = subject_of(account)
        stamp = (account.last_login.timestamp() if account.last_login else 0, account.pk)
        if sub not in latest or stamp > latest[sub][0]:
            latest[sub] = (stamp, teams)
    if not latest:
        return None
    merged = {}
    for _stamp, teams in latest.values():
        for team in teams:
            merged.setdefault(team["slug"], team)
    return sorted(merged.values(), key=lambda t: t["slug"])


def _flow(mode: str, queue: list[str]) -> dict:
    return {
        "mode": mode,
        "queue": queue,
        "pending": None,
        "connected": [],
        "stopped": None,
        "updated_at": time.time(),
    }


def start_all(unconnected: list[str]) -> dict:
    return _flow(MODE_ALL, list(unconnected))


def note_started(flow: dict | None, team: str) -> dict:
    """Record that the login view is sending the browser to OCS for ``team``.

    A pinned hop that is not the head of a running chain is a one-off connect and
    replaces whatever flow was there. The head being started again (a second tab)
    keeps the chain.
    """
    if (
        flow
        and flow.get("mode") == MODE_ALL
        and not flow.get("stopped")
        and flow.get("pending") in (None, team)
        and flow.get("queue")
        and flow["queue"][0] == team
    ):
        return {**flow, "pending": team, "updated_at": time.time()}
    return {**_flow(MODE_ONE, [team]), "pending": team}


def stop(flow: dict | None, reason: str, team: str, got: str = "") -> dict:
    """Stop the flow at ``team``, keeping what it has already connected."""
    flow = flow or _flow(MODE_ONE, [team])
    stopped = {"reason": reason, "team": team}
    if got:
        stopped["got"] = got
    return {**flow, "pending": None, "stopped": stopped}


def reconcile(flow: dict | None, connected: set[str], now: float | None = None) -> dict | None:
    """Settle the in-flight hop against the teams the user now has connected.

    A pending team that is now connected came back from OCS; one that still is not
    after a short grace never did (cancelled or failed somewhere allauth could not
    report), so the chain stops rather than retrying it. Returns None once a one-off connect has succeeded.
    """
    if not flow:
        return None
    now = time.time() if now is None else now
    flow = {**flow, "queue": list(flow.get("queue") or [])}
    idle_for = now - flow.get("updated_at", 0)
    pending = flow.get("pending")
    if pending:
        if pending in connected:
            flow["connected"] = [*flow.get("connected", []), pending]
            flow["pending"] = None
            flow["updated_at"] = now
            idle_for = 0
        elif idle_for > PENDING_GRACE_SECONDS:
            flow = stop(flow, STOP_INCOMPLETE, pending)
    flow["queue"] = [slug for slug in flow["queue"] if slug not in connected]
    upcoming = next_team(flow)
    if upcoming and idle_for > CHAIN_IDLE_SECONDS:
        flow = stop(flow, STOP_IDLE, upcoming)
    if flow.get("mode") == MODE_ONE and not flow.get("stopped") and not flow["queue"]:
        return None
    return flow


def next_team(flow: dict | None) -> str | None:
    if (
        flow
        and flow.get("mode") == MODE_ALL
        and not flow.get("stopped")
        and not flow.get("pending")
        and flow.get("queue")
    ):
        return flow["queue"][0]
    return None


def is_finished(flow: dict | None) -> bool:
    return bool(
        flow and flow.get("mode") == MODE_ALL and not flow.get("stopped") and not flow.get("queue")
    )
