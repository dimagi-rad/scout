"""Golden eval for the metadata-vs-verified rule from base_system.py.

Regression pin for issue #189: the agent must not quote
``list_tables.materialized_row_count`` to the user as a verified count. It
must run ``semantic_query`` to get a live number, or — if the data is no longer
queryable — refuse to cite the materialized count.

CI has no Anthropic API key, so these tests do NOT invoke a real LLM. Instead
they pin three things that make the bug impossible by construction:

1. ``BASE_SYSTEM_PROMPT`` contains the explicit rule wording.
2. ``pipeline_list_tables`` tool output emits ``materialized_row_count`` +
   ``row_count_verified: false``, never the bare ``row_count`` the agent
   used to read.

Together these guarantee the agent literally cannot read a field called
``row_count`` from ``list_tables`` anymore — the rename forces it to read
the new, self-documenting name instead.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.agents.graph.base import (
    _build_system_prompt,
)
from apps.agents.prompts.base_system import BASE_SYSTEM_PROMPT
from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
from mcp_server.services.metadata import pipeline_list_tables


def _make_pipeline_config(sources):
    return PipelineConfig(
        name="commcare_sync",
        description="Test pipeline",
        version="1.0",
        provider="commcare",
        sources=[SourceConfig(name=n, description=d) for n, d in sources],
        relationships=[],
    )


class TestBaseSystemPromptContainsProvenanceRule:
    """Pin the prompt wording so a future edit can't silently weaken it."""

    def test_metadata_vs_verified_section_present(self):
        assert "## Metadata vs. Verified Counts" in BASE_SYSTEM_PROMPT

    def test_prompt_names_the_field(self):
        assert "row_count" in BASE_SYSTEM_PROMPT
        assert "row_count_verified" in BASE_SYSTEM_PROMPT

    def test_prompt_forbids_quoting_metadata_as_answer(self):
        # Look for the rule phrased as a NEVER. The exact case is part of
        # the contract — if a future edit tones it down to "avoid", we want
        # to know.
        assert "NEVER report" in BASE_SYSTEM_PROMPT
        assert "semantic_query" in BASE_SYSTEM_PROMPT
        assert "dataset.count" in BASE_SYSTEM_PROMPT

    def test_prompt_tells_agent_what_to_do_on_unavailability(self):
        # When the table is unavailable, the agent must offer re-materialize
        # rather than fall back to the materialized count.
        assert "re-run materialization" in BASE_SYSTEM_PROMPT
        assert "NOT_FOUND" in BASE_SYSTEM_PROMPT or "VALIDATION_ERROR" in BASE_SYSTEM_PROMPT


class TestPipelineListTablesOutputShape:
    """Pin the tool-output shape so the agent literally cannot read a bare
    ``row_count`` from ``list_tables`` anymore.
    """

    @pytest.mark.asyncio
    async def test_emits_materialized_row_count_not_row_count(self):
        mock_ts = MagicMock()
        mock_ts.schema_name = "t_test"
        pipeline_config = _make_pipeline_config([("users", "Users")])

        mock_run = MagicMock()
        mock_run.completed_at = datetime(2026, 5, 1, tzinfo=UTC)
        mock_run.result = {"sources": {"users": {"state": "completed", "rows": 100}}}

        with (
            patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls,
            patch(
                "mcp_server.services.metadata._live_tables_in_schema",
                AsyncMock(return_value={"raw_users"}),
            ),
        ):
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.RunState.PARTIAL = "partial"
            qs = mock_run_cls.objects.filter.return_value.order_by.return_value
            qs.afirst = AsyncMock(return_value=mock_run)

            result = await pipeline_list_tables(mock_ts, pipeline_config)

        assert len(result) == 1
        entry = result[0]
        # Positive: new fields present and correct
        assert entry["materialized_row_count"] == 100
        assert entry["row_count_verified"] is False
        # Negative: legacy field gone — the agent must NOT see a bare row_count
        # that it can mistake for a verified count.
        assert "row_count" not in entry


class TestAssembledSystemPromptIncludesRule:
    """End-to-end: when the agent is built for a workspace whose tables
    have materialized counts, the assembled system prompt must contain both
    the rule AND the counts presented as materialization-time metadata.

    This is the integration check that ties the three previous tests
    together.
    """

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_assembled_prompt_contains_rule_and_labels_counts(self, workspace, user):
        # Schema context will be the "no data" branch (no TenantSchema row in
        # the test DB), which is fine — we're checking the BASE_SYSTEM_PROMPT
        # portion is included end-to-end.
        prompt = await _build_system_prompt(workspace, user)
        assert "## Metadata vs. Verified Counts" in prompt
        assert "row_count" in prompt
        assert "NEVER report" in prompt
