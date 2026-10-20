"""Provider descriptions of a source (OCS chatbot, Connect opportunity), #917.

Neither provider's list endpoint carries one (as of 2026-10): OCS embeds a
chatbot's description only in the experiment stub of ``/api/sessions/``, and
Connect only in the per-opportunity export. So OCS is probed one session per
chatbot in a background task after discovery, and Connect's is copied from the
metadata a load fetches.

A stored description is never cleared by a probe that could not see one, so
one removed upstream lingers until the provider sends a new one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.credential_resolver import CredentialResolutionError, aresolve_credential
from apps.users.services.tenant_listing.rows import clean_description

logger = logging.getLogger(__name__)

# Off the request path, but each probe is a paginated sessions query on OCS
# (rate-limited per token), so keep it gentle and bounded.
_OCS_PROBE_BUDGET_SECONDS = 60.0
_OCS_PROBE_CONCURRENCY = 4
_OCS_PROBE_TIMEOUT_SECONDS = 10.0


def connect_description(opportunity: Any) -> str | None:
    """A Connect opportunity export's description, else its short description."""
    if not isinstance(opportunity, Mapping):
        return None
    return clean_description(opportunity.get("description")) or clean_description(
        opportunity.get("short_description")
    )


@dataclass(frozen=True)
class ChatbotProbe:
    """What a chatbot's newest session says about it; both None with no sessions."""

    description: str | None
    last_session_at: str | None


async def _probe_one(
    client: httpx.AsyncClient, url: str, headers: Mapping[str, str], experiment_id: str
) -> ChatbotProbe | None:
    try:
        response = await client.get(
            url, headers=dict(headers), params={"experiment": experiment_id, "page_size": 1}
        )
        if response.status_code != 200:
            return None
        results = response.json().get("results")
    except Exception:
        # Left out rather than raised: one chatbot's probe must not cost the others.
        logger.debug("OCS probe failed for chatbot %s", experiment_id, exc_info=True)
        return None
    if not isinstance(results, list):
        return None
    if not results or not isinstance(results[0], dict):
        return ChatbotProbe(None, None)
    session = results[0]
    experiment = session.get("experiment")
    if not isinstance(experiment, dict) or str(experiment.get("id")) != experiment_id:
        # Not this chatbot's session: the filter was ignored, so learn nothing.
        return None
    description = clean_description(experiment.get("description"))
    created_at = session.get("created_at")
    return ChatbotProbe(description, created_at if isinstance(created_at, str) else None)


async def aprobe_ocs_chatbots(
    base_url: str, headers: Mapping[str, str], experiment_ids: Iterable[str]
) -> dict[str, ChatbotProbe]:
    """Each chatbot's description and newest session time, best effort.

    A chatbot whose probe failed or ran past the budget is left out, so callers
    keep what they stored before rather than blanking it.
    """
    ids = list(dict.fromkeys(experiment_ids))
    if not ids:
        return {}
    url = f"{base_url.rstrip('/')}/api/sessions/"
    semaphore = asyncio.Semaphore(_OCS_PROBE_CONCURRENCY)
    probes: dict[str, ChatbotProbe] = {}

    async with httpx.AsyncClient(timeout=_OCS_PROBE_TIMEOUT_SECONDS) as client:

        async def probe(experiment_id: str) -> None:
            async with semaphore:
                result = await _probe_one(client, url, headers, experiment_id)
            if result is not None:
                probes[experiment_id] = result

        tasks = [asyncio.create_task(probe(experiment_id)) for experiment_id in ids]
        _done, pending = await asyncio.wait(tasks, timeout=_OCS_PROBE_BUDGET_SECONDS)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            logger.info("OCS chatbot probe budget ran out with %d left", len(pending))
    return probes


async def arecord_ocs_descriptions(
    base_url: str, headers: Mapping[str, str], experiment_ids: Iterable[str], server=""
) -> None:
    """Probe and store chatbot descriptions; never raises, as they only decorate."""
    try:
        probes = await aprobe_ocs_chatbots(base_url, headers, experiment_ids)
        await astore_descriptions(
            "ocs", {eid: probe.description for eid, probe in probes.items()}, server
        )
    except Exception:
        logger.warning("Could not record OCS chatbot descriptions", exc_info=True)


async def aprobe_connection_chatbots(connection_id: str, base_url: str) -> None:
    """Probe every live chatbot an OCS connection reaches, with its own credential."""
    memberships = [
        tm
        async for tm in TenantMembership.objects.filter(
            connection_id=connection_id,
            connection__provider="ocs",
            archived_at__isnull=True,
        ).select_related("connection", "user", "tenant")
    ]
    if not memberships:
        return
    try:
        credential = await aresolve_credential(memberships[0])
    except CredentialResolutionError:
        credential = None
    if not credential:
        logger.info("No usable credential to probe OCS connection %s", connection_id)
        return
    if credential["type"] == TenantConnection.API_KEY:
        headers = {"X-api-key": credential["value"]}
    else:
        headers = {"Authorization": f"Bearer {credential['value']}"}
    by_server: dict[str, list[str]] = {}
    for tm in memberships:
        by_server.setdefault(tm.tenant.server, []).append(tm.tenant.external_id)
    for server, experiment_ids in by_server.items():
        await arecord_ocs_descriptions(base_url, headers, experiment_ids, server)


async def astore_descriptions(provider: str, descriptions: Mapping[str, str | None], server=""):
    """Save each found description; a missing one never clears what is stored."""
    for external_id, description in descriptions.items():
        if description:
            await (
                Tenant.objects.filter(provider=provider, server=server, external_id=external_id)
                .exclude(description=description)
                .aupdate(description=description)
            )
