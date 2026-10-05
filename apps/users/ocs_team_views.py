"""The OCS teams a user can still connect, and the "connect all" chain over them."""

from __future__ import annotations

from allauth.socialaccount.models import SocialToken
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

    stopped = flow.get("stopped")
    return {
        "mode": flow["mode"],
        "connected": [team(s) for s in flow.get("connected", [])],
        "remaining": [team(s) for s in flow.get("queue", [])],
        "finished": finished,
        "stopped": stopped
        and {
            "reason": stopped["reason"],
            "team": team(stopped["team"]),
            "got": team(stopped["got"]) if stopped.get("got") else None,
        },
    }


async def _arespond(request, flow, teams, connected):
    finished = ocs_team_flow.is_finished(flow)
    # A finished chain is reported once, then forgotten.
    if flow and not finished:
        await request.session.aset(ocs_team_flow.SESSION_KEY, flow)
    else:
        await request.session.apop(ocs_team_flow.SESSION_KEY, None)
    names = {t["slug"]: t["name"] for t in teams or []}
    return JsonResponse(
        {
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
    unconnected = [t["slug"] for t in teams or [] if t["slug"] not in connected]
    if not unconnected:
        return JsonResponse({"error": "No unconnected Open Chat Studio teams."}, status=400)
    return await _arespond(request, ocs_team_flow.start_all(unconnected), teams, connected)


@require_http_methods(["POST"])
@async_login_required
async def ocs_teams_stop_view(request):
    """Stop a running chain before its next hop, keeping what it connected."""
    teams, connected = await _ateam_state(request._authenticated_user)
    flow = ocs_team_flow.reconcile(await request.session.aget(ocs_team_flow.SESSION_KEY), connected)
    upcoming = ocs_team_flow.next_team(flow)
    if upcoming:
        flow = ocs_team_flow.stop(flow, ocs_team_flow.STOP_USER, upcoming)
    return await _arespond(request, flow, teams, connected)


@require_http_methods(["POST"])
@async_login_required
async def ocs_teams_dismiss_view(request):
    """Forget the flow and its message."""
    await request.session.apop(ocs_team_flow.SESSION_KEY, None)
    return JsonResponse({"status": "dismissed"})
