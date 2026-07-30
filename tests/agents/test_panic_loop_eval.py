"""Golden eval for the panic-loop / escalation rule from base_system.py.

Regression pin for issue #190: when ``list_tables`` and the database disagree
(NOT_FOUND / VALIDATION_ERROR on the same table), the agent must stop
exploring after a small number of attempts rather than ratholing into
``pg_catalog`` and consuming the recursion budget.

CI has no Anthropic API key, so these tests do NOT invoke a real LLM. They
pin the structural pieces that make the bug impossible by construction:

1. ``BASE_SYSTEM_PROMPT`` contains the explicit "When the Schema is Broken"
   section, the STOP-exploring rule, and the explicit prohibition on
   ``pg_catalog`` / ``pg_namespace`` exploration.
2. ``_build_system_prompt`` includes the section end-to-end.
"""

import pytest

from apps.agents.graph.base import _build_system_prompt
from apps.agents.prompts.base_system import BASE_SYSTEM_PROMPT


class TestBaseSystemPromptContainsEscalationSection:
    """Pin the prompt wording so a future edit can't silently weaken it."""

    def test_when_schema_is_broken_section_present(self):
        assert "## When the Schema is Broken" in BASE_SYSTEM_PROMPT

    def test_prompt_names_the_error_codes(self):
        # The rule keys off these two MCP error codes. If they're ever
        # renamed, the prompt must be updated in lockstep.
        assert "NOT_FOUND" in BASE_SYSTEM_PROMPT
        assert "VALIDATION_ERROR" in BASE_SYSTEM_PROMPT

    def test_prompt_says_stop_exploring(self):
        # The exact case is part of the contract — if a future edit tones
        # it down to "consider stopping" or similar, we want to know.
        assert "STOP exploring" in BASE_SYSTEM_PROMPT

    def test_prompt_directs_to_run_materialization(self):
        # The escalation path is to call run_materialization OR ask the
        # user. Both must be reachable from the rule.
        assert "run_materialization" in BASE_SYSTEM_PROMPT
        assert "re-materialize" in BASE_SYSTEM_PROMPT


class TestBaseSystemPromptForbidsPgCatalogExploration:
    """The observed bug was the agent running 6+ pg_catalog queries. Pin the
    explicit prohibition so a future "be more helpful" edit can't drop it.
    """

    def test_prompt_forbids_pg_catalog_tables(self):
        # Each system catalog the agent reached for in the incident must be
        # named explicitly. Naming them is what makes the rule enforceable.
        for table in ("pg_namespace", "pg_class", "pg_views", "pg_tables"):
            assert table in BASE_SYSTEM_PROMPT, f"Missing prohibition on {table}"

    def test_prompt_limits_alternate_query_attempts(self):
        # "More than two query attempts" is the operational rule — not
        # "give up immediately" (one NOT_FOUND can be a typo).
        assert "more than two" in BASE_SYSTEM_PROMPT.lower()


class TestAssembledSystemPromptIncludesEscalationRule:
    """End-to-end: when the agent is built for a workspace, the assembled
    system prompt must contain the escalation rule.
    """

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_assembled_prompt_contains_escalation_section(self, workspace, user):
        # _build_system_prompt returns a (stable, volatile) split (arch #254).
        prompt = "\n".join(await _build_system_prompt(workspace, user))
        assert "## When the Schema is Broken" in prompt
        assert "STOP exploring" in prompt
        assert "pg_namespace" in prompt
