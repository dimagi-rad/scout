"""Golden eval for KK's revisit-opportunity question (#919).

KK asked, in the R1 revisit workspace, to compare it with the primary
opportunity, which was not in the workspace, and noted the projection rules
still had to be decided. Scout answered about R1 alone, never flagged the
missing opportunity, never asked about projection rules, and ended on a
generic "What would you like to look at?" menu.

CI has no Anthropic API key, so these tests pin the structure that makes the
right answer reachable: the prompt names the workspace's sources, tells the
agent to flag and offer to add a named source missing from them, to ask about
the specific missing assumption, and never to fall back to a generic menu.
The history half (the question surviving a long tool turn) is pinned in
test_prune_messages.py with the same question.
"""

import pytest
from asgiref.sync import sync_to_async

from apps.agents.graph import prompt_context
from apps.agents.graph.prompt_context import MAX_LISTED_SOURCES, _build_system_prompt
from apps.agents.prompts.base_system import (
    BASE_SYSTEM_PROMPT,
    HEADLESS_BASE_SYSTEM_PROMPT,
    READ_ONLY_BASE_SYSTEM_PROMPT,
)
from apps.users.models import Tenant
from apps.workspaces.models import WorkspaceTenant
from tests.agents.test_prune_messages import KK_QUESTION

PRIMARY_OPP = "CHC - NG - EHA - RCT - AUG 26"
REVISIT_OPP = "CHC - NG - EHA - RCT - AUG 26 - R1"

ALL_PROMPTS = [BASE_SYSTEM_PROMPT, HEADLESS_BASE_SYSTEM_PROMPT, READ_ONLY_BASE_SYSTEM_PROMPT]


def test_kk_question_names_both_opportunities():
    assert PRIMARY_OPP in KK_QUESTION
    assert REVISIT_OPP in KK_QUESTION


@pytest.mark.parametrize("prompt", ALL_PROMPTS)
def test_prompt_flags_sources_missing_from_workspace(prompt):
    assert "## When a Question Names Data Outside This Workspace" in prompt
    assert "Workspace Sources" in prompt
    assert "Say so first, naming it" in prompt
    assert "Add data source" in prompt
    assert "`list_workspaces`" in prompt


@pytest.mark.parametrize("prompt", ALL_PROMPTS)
def test_prompt_asks_about_missing_assumptions(prompt):
    assert "only partly launched" in prompt
    assert "Ask about the specific assumption" in prompt
    assert "never reply with a generic menu" in prompt


@pytest.mark.parametrize("prompt", ALL_PROMPTS)
def test_prompt_names_sources_behind_an_answer(prompt):
    assert "name the source(s) from Workspace Sources the answer covers" in prompt


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_assembled_prompt_lists_only_this_workspaces_sources(workspace, user):
    """KK's setup: R1 in the workspace, the primary opportunity accessible but not added."""
    prompt_context._system_prompt_cache.clear()
    revisit = await Tenant.objects.acreate(
        provider="commcare_connect", external_id="9001", canonical_name=REVISIT_OPP
    )
    await Tenant.objects.acreate(
        provider="commcare_connect", external_id="9000", canonical_name=PRIMARY_OPP
    )
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=revisit)

    stable, volatile = await _build_system_prompt(workspace, user)

    assert "### Workspace Sources" in volatile
    sources = volatile.split("### Workspace Sources", 1)[1].split("###", 1)[0]
    assert f"- {REVISIT_OPP} (" in sources
    assert "9001" in sources
    assert f"- {PRIMARY_OPP} (" not in sources
    assert "## When a Question Names Data Outside This Workspace" in stable


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_long_source_list_is_capped(workspace, user):
    prompt_context._system_prompt_cache.clear()

    @sync_to_async
    def add_sources():
        for i in range(MAX_LISTED_SOURCES + 3):
            tenant = Tenant.objects.create(
                provider="commcare", external_id=f"d{i:03}", canonical_name=f"Domain {i:03}"
            )
            WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)

    await add_sources()

    _, volatile = await _build_system_prompt(workspace, user)

    assert "- and 4 more; `list_workspaces` lists them all" in volatile
