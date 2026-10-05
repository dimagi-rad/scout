"""The OCS teams a user can still connect, and the "connect all" chain over them."""

from __future__ import annotations

import json

from allauth.socialaccount.models import SocialToken
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from apps.users.decorators import async_login_required
from apps.users.services import ocs_team_flow
from apps.users.services.oauth_scope import account_scope, provider_accounts


async def _ateam_state(user):
    """(known teams or None, slugs of teams with a live token)."""
    accounts = [account async for account in provider_accounts(user.pk, "ocs")]
    # A removed connection deletes its tokens but keeps the SocialAccount.
    with_token = {
        account_id
        async for account_id in SocialToken.objects.filter(
            account__in=[a.pk for a in accounts]
        ).values_list("account_id", flat=True)
    }
    connected = {account_scope(a) for a in accounts if a.pk in with_token} - {""}
    return ocs_team_flow.known_teams(accounts), connected


def _flow_payload(flow, names, *, finished):
    if not flow:
        return None

    def team(slug):
        return {"slug": slug, "name": names.get(slug, slug)}

    stopped = flow.get("stopped") or {}
    return {
        "mode": flow.get("mode", ocs_team_flow.MODE_ONE),
        "connected": [team(s) for s in flow.get("connected") or []],
        "remaining": [team(s) for s in flow.get("queue") or []],
        "pending": team(flow["pending"]) if flow.get("pending") else None,
        "finished": finished,
        "stopped": (
            stopped.get("team")
            and {
                "reason": stopped.get("reason", ocs_team_flow.STOP_FAILED),
                "team": team(stopped["team"]),
                "got": team(stopped["got"]) if stopped.get("got") else None,
            }
        )
        or None,
    }


async def _arespond(request, flow, teams, connected):
    finished = ocs_team_flow.is_finished(flow)
    # A finished chain is reported once, then forgotten.
    keep = flow if flow and not finished else None
    if keep != await request.session.aget(ocs_team_flow.SESSION_KEY):
        if keep:
            await request.session.aset(ocs_team_flow.SESSION_KEY, keep)
        else:
            await request.session.apop(ocs_team_flow.SESSION_KEY, None)
    names = {t["slug"]: t["name"] for t in teams or []}
    return JsonResponse(
        {
            # Without the scope no fresh list can arrive, so a reconnect hint can't help.
            "available": settings.OCS_REQUEST_TEAMS_SCOPE,
            "known": teams is not None,
            "teams": [{**t, "connected": t["slug"] in connected} for t in teams or []],
            "flow": _flow_payload(flow, names, finished=finished),
            "next": ocs_team_flow.next_team(flow),
        }
    )


@require_http_methods(["GET"])
@async_login_required
async def ocs_teams_view(request):
    """The user's OCS teams, and where any connect flow got to.

    Reading it settles the hop the browser just came back from, so the page learns
    which team (if any) to start next.
    """
    teams, connected = await _ateam_state(request._authenticated_user)
    flow = ocs_team_flow.reconcile(await request.session.aget(ocs_team_flow.SESSION_KEY), connected)
    return await _arespond(request, flow, teams, connected)


@require_http_methods(["POST"])
@async_login_required
async def ocs_teams_connect_all_view(request):
    """Queue every known, unconnected team; the page then starts the first hop."""
    teams, connected = await _ateam_state(request._authenticated_user)
    if teams is None:
        return JsonResponse(
            {"error": "Reconnect an Open Chat Studio team first to list your teams."},
            status=400,
        )
    unconnected = [t["slug"] for t in teams if t["slug"] not in connected]
    if not unconnected:
        return JsonResponse({"error": "No unconnected Open Chat Studio teams."}, status=400)
    return await _arespond(request, ocs_team_flow.start_all(unconnected), teams, connected)


@require_http_methods(["POST"])
@async_login_required
async def ocs_teams_stop_view(request):
    """Stop a running chain, keeping what it connected.

    ``{"reason": "failed"}`` records a hop the browser couldn't start. A hop still
    pending is stopped too: if it lands anyway its team is simply connected.
    """
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        body = {}
    reason = (
        ocs_team_flow.STOP_FAILED
        if isinstance(body, dict) and body.get("reason") == "failed"
        else ocs_team_flow.STOP_USER
    )
    teams, connected = await _ateam_state(request._authenticated_user)
    flow = ocs_team_flow.reconcile(await request.session.aget(ocs_team_flow.SESSION_KEY), connected)
    team = (flow or {}).get("pending") or ocs_team_flow.next_team(flow)
    if team:
        flow = ocs_team_flow.stop(flow, reason, team)
    return await _arespond(request, flow, teams, connected)


@require_http_methods(["POST"])
@async_login_required
async def ocs_teams_dismiss_view(request):
    """Forget the flow and its message."""
    await request.session.apop(ocs_team_flow.SESSION_KEY, None)
    return JsonResponse({"status": "dismissed"})
