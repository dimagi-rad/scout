"""Tests for M3 Task 9: agent routing to governed measures + model-gap logging.

Verifies that:
1. The assembled system prompt contains routing guidance that instructs the
   agent to prefer ``semantic_query`` for metric-style questions and use the
   raw ``query`` tool only as a fallback.
2. ``record_fallback`` persists a ``ModelGapSignal`` row with the correct
   workspace, question, and SQL fields.
"""

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")


class TestSemanticRoutingPrompt:
    """The system prompt must contain governed-layer routing instructions."""

    @pytest.mark.django_db(transaction=True)
    async def test_prompt_mentions_semantic_query(self, workspace, user):
        """The assembled system prompt must reference ``semantic_query``."""
        from apps.agents.graph.base import _build_system_prompt

        prompt = await _build_system_prompt(workspace, user)

        assert "semantic_query" in prompt, (
            "System prompt does not mention 'semantic_query'; "
            "routing guidance was not injected."
        )

    @pytest.mark.django_db(transaction=True)
    async def test_prompt_instructs_prefer_governed_layer(self, workspace, user):
        """The system prompt must instruct the agent to prefer the semantic layer."""
        from apps.agents.graph.base import _build_system_prompt

        prompt = await _build_system_prompt(workspace, user)

        # The guidance uses "PREFER" in the text — assert it contains a prefer instruction.
        assert "PREFER" in prompt or "prefer" in prompt.lower(), (
            "System prompt does not contain a 'prefer' instruction for the "
            "governed semantic layer."
        )

    @pytest.mark.django_db(transaction=True)
    async def test_prompt_mentions_fallback_to_raw_sql(self, workspace, user):
        """The system prompt must mention when the raw ``query`` tool should be used."""
        from apps.agents.graph.base import _build_system_prompt

        prompt = await _build_system_prompt(workspace, user)

        # The guidance says to fall back to raw `query` only when no measure fits.
        lower = prompt.lower()
        assert "raw" in lower or "fallback" in lower or "fall back" in lower, (
            "System prompt does not describe a fallback-to-raw-SQL path."
        )

    @pytest.mark.django_db(transaction=True)
    async def test_prompt_mentions_semantic_catalog(self, workspace, user):
        """The system prompt should mention ``semantic_catalog`` as the discovery tool."""
        from apps.agents.graph.base import _build_system_prompt

        prompt = await _build_system_prompt(workspace, user)

        assert "semantic_catalog" in prompt, (
            "System prompt does not mention 'semantic_catalog'; "
            "the agent cannot discover available measures."
        )

    def test_base_system_prompt_contains_routing_guidance(self):
        """BASE_SYSTEM_PROMPT itself (not just the assembled prompt) must have the
        routing section so it is always present regardless of workspace config."""
        from apps.agents.prompts.base_system import BASE_SYSTEM_PROMPT

        assert "semantic_query" in BASE_SYSTEM_PROMPT
        assert "semantic_catalog" in BASE_SYSTEM_PROMPT
        # Must describe the preference rule, not just name the tool.
        assert "PREFER" in BASE_SYSTEM_PROMPT or "prefer" in BASE_SYSTEM_PROMPT.lower()


class TestModelGapSignal:
    """``record_fallback`` must persist a ModelGapSignal row."""

    @pytest.mark.django_db(transaction=True)
    async def test_record_fallback_creates_row(self, workspace):
        """record_fallback writes a ModelGapSignal with the expected field values."""
        from apps.agents.services.fallback_log import record_fallback
        from apps.knowledge.models import ModelGapSignal

        question = "how many visits?"
        sql = "SELECT COUNT(*) FROM visits"

        signal = await record_fallback(workspace, question, sql)

        assert signal.pk is not None, "ModelGapSignal was not saved (pk is None)."
        assert signal.workspace_id == workspace.pk
        assert signal.question == question
        assert signal.sql == sql
        assert signal.created_at is not None

        # Confirm it is persisted to the database.
        count = await ModelGapSignal.objects.filter(pk=signal.pk).acount()
        assert count == 1, "ModelGapSignal row not found in the database after record_fallback."

    @pytest.mark.django_db(transaction=True)
    async def test_record_fallback_empty_sql(self, workspace):
        """record_fallback works when sql is omitted (defaults to empty string)."""
        from apps.agents.services.fallback_log import record_fallback

        signal = await record_fallback(workspace, "total revenue?")

        assert signal.sql == ""
        assert signal.question == "total revenue?"

    @pytest.mark.django_db(transaction=True)
    async def test_record_fallback_null_workspace(self):
        """record_fallback works with workspace=None (workspace field is nullable)."""
        from apps.agents.services.fallback_log import record_fallback

        signal = await record_fallback(None, "orphan question", "SELECT 1")

        assert signal.workspace_id is None
        assert signal.question == "orphan question"
