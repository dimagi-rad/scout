#!/usr/bin/env python
"""DDD setup for crossopp-redefine-measure-from-chat.

Produces the SEEDED-BROKEN state the demo opens on, then writes outputs.json:
  - the PIPN cross-opp workspace (opps 10019 + 10021) with the DEFAULT resolution
    (age_days -> child_age), which makes the growth curve flat;
  - a chat thread in which the agent built the curve: it defines age_days (resolving to the
    wrong child_age field, rendering the lineage card with the "Edit definition" button) and
    creates the flat growth-curve artifact.

Run from the repo root:  uv run python scripts/walkthroughs/redefine-measure/seed.py
"""

import asyncio
import json
import os
import uuid
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
django.setup()

from django.conf import settings  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402

from apps.transformations.models import CrossOppMeasureLineage  # noqa: E402
from apps.transformations.services import crossopp_measure_service as svc  # noqa: E402
from apps.workspaces.models import Workspace  # noqa: E402
from apps.workspaces.tasks import _build_agent_for_resume  # noqa: E402

WS_ID = "168a38b8-3f9f-4a6d-8b25-21b46b1d40f7"
OUT = Path(__file__).parent / "outputs.json"


async def main():
    ws = await Workspace.objects.select_related("created_by").aget(id=WS_ID)
    user = ws.created_by

    # 1. Reset to the seeded-broken state: drop age_days so the agent re-defines it on camera
    #    (rendering the lineage card whose "Edit definition" button the demo clicks), and so
    #    its natural resolution is the WRONG child_age field.
    opps, _ = await asyncio.to_thread(svc.workspace_opps, ws)
    await CrossOppMeasureLineage.objects.filter(workspace=ws, measure="age_days").adelete()
    await asyncio.to_thread(svc.regenerate_model, ws, opps)

    # 2. Drive the agent to build the growth curve -> defines age_days (child_age) + flat artifact
    thread_id = str(uuid.uuid4())
    agent, oauth_tokens = await _build_agent_for_resume(ws, user, conversation_id=thread_id)
    msg = (
        "Set up an infant growth curve for this workspace. Do EXACTLY these steps and nothing "
        "else:\n"
        "1. Call define_crossopp_visit_field for 'visit_weight' and for 'age_days', and "
        "define_crossopp_measure for 'birth_weight'. Accept whatever column the resolver picks "
        "for each — do NOT inspect, correct, change, or redefine any field, and do NOT call "
        "redefine_crossopp_visit_field. If a field already exists, that is fine.\n"
        "2. Run ONE semantic_query for average visit weight by age_week (weeks 0-6) per "
        "birth-weight band.\n"
        "3. Call create_artifact to make the growth-curve chart from that query.\n"
        "The curve may look flat — that is expected and you must NOT try to fix it. Stop after "
        "creating the artifact."
    )
    state = {
        "messages": [HumanMessage(content=msg)],
        "workspace_id": WS_ID,
        "user_id": str(user.id),
        "user_role": "analyst",
        "thread_id": thread_id,
    }
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": settings.AGENT_RESUME_RECURSION_LIMIT,
        "oauth_tokens": oauth_tokens,
    }
    result = await agent.ainvoke(state, config)
    called = [
        tc.get("name")
        for m in result.get("messages", [])
        for tc in (getattr(m, "tool_calls", None) or [])
    ]

    # ainvoke populates the checkpointer but not the Thread DB row; the messages endpoint
    # returns [] without it. Create the row so the seeded thread is visible in the UI.
    from apps.chat.models import Thread

    await Thread.objects.aupdate_or_create(
        id=thread_id,
        create_defaults={
            "user": user,
            "workspace": ws,
            "title": "Infant growth curve",
        },
    )

    # 3. FORCE the seeded-broken state deterministically: age_days -> child_age, regardless of
    #    what the agent did (it sometimes proactively redefines). The demo must open flat.
    from apps.transformations.services.measure_resolver import MeasureResolution

    broken = {
        opp.external_id: MeasureResolution(
            measure="age_days", column="child_age", source_path=None, sql_expression="child_age",
            confidence=0.6, status="resolved", matched_label="child_age (field)", reason="seed default",
        )
        for opp in opps
    }
    await asyncio.to_thread(svc.add_visit_field, ws, "age_days", broken, opps)

    # 4. Require the flat artifact (scene 1 opens it); fail loudly if the agent skipped it.
    from apps.artifacts.models import Artifact

    art = await Artifact.objects.filter(workspace=ws).order_by("-created_at").afirst()
    age_rows = [
        r async for r in CrossOppMeasureLineage.objects.filter(workspace=ws, measure="age_days")
    ]
    OUT.write_text(json.dumps({"ws_id": WS_ID, "build_thread_id": thread_id}, indent=2) + "\n")
    print("tools called:", called)
    print("age_days reset to child_age:", {r.opportunity_id: r.column for r in age_rows})
    print("artifact present:", bool(art), art.id if art else None)
    print("outputs:", OUT.read_text().strip())
    if not art or "create_artifact" not in called:
        raise SystemExit("SEED INCOMPLETE: agent did not create the growth-curve artifact — re-run")


if __name__ == "__main__":
    asyncio.run(main())
