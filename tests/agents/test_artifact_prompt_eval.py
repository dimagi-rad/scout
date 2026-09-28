"""Prompt contract checks for semantic graph artifact creation."""

import pytest

from apps.agents.prompts.artifact_prompt import ARTIFACT_PROMPT_ADDITION
from apps.agents.prompts.base_system import (
    BASE_SYSTEM_PROMPT,
    HEADLESS_BASE_SYSTEM_PROMPT,
    READ_ONLY_BASE_SYSTEM_PROMPT,
)
from apps.agents.tools.artifact_manager_agent import (
    ARTIFACT_MANAGER_SYSTEM_PROMPT,
    NESTED_MCP_TOOL_NAMES,
)
from apps.agents.tools.canvas_manager_agent import CANVAS_MANAGER_SYSTEM_PROMPT


def test_artifact_prompt_lists_narrative_block_config_keys():
    old_manager_name = "`artifact_" + "graph_manager`"

    assert "`artifact_manager`" in ARTIFACT_PROMPT_ADDITION
    assert old_manager_name not in ARTIFACT_PROMPT_ADDITION
    assert "Block config keys:" in ARTIFACT_PROMPT_ADDITION
    assert "call `artifact_manager` first" in ARTIFACT_PROMPT_ADDITION
    assert "Do not preflight the task" in ARTIFACT_PROMPT_ADDITION
    assert "`section`: `title`, `body`" in ARTIFACT_PROMPT_ADDITION
    assert "`question`: `text`, optional `context`" in ARTIFACT_PROMPT_ADDITION
    assert "`tldr`: optional `title`" in ARTIFACT_PROMPT_ADDITION
    assert "`markdown`: `body` or `content`" in ARTIFACT_PROMPT_ADDITION
    assert "Do not use `text`" in ARTIFACT_PROMPT_ADDITION


def test_artifact_prompts_require_recharts_and_forbid_plotly_output():
    assert "All charts render with Recharts" in ARTIFACT_PROMPT_ADDITION
    assert "Never create or request a Plotly artifact" in ARTIFACT_PROMPT_ADDITION
    assert "Render every chart with Recharts" in ARTIFACT_MANAGER_SYSTEM_PROMPT
    assert "Plotly is not available" in ARTIFACT_MANAGER_SYSTEM_PROMPT


def test_artifact_prompts_define_bounded_visualization_grammar():
    for prompt in (ARTIFACT_PROMPT_ADDITION, ARTIFACT_MANAGER_SYSTEM_PROMPT):
        assert "categorical" in prompt
        assert "monochrome" in prompt
        assert "horizontal" in prompt
        assert "percent" in prompt
        assert "neutral" in prompt
        assert "long labels" in prompt


def test_artifact_manager_prompt_documents_atomic_apply_ops():
    assert '"op":"set"' in ARTIFACT_MANAGER_SYSTEM_PROMPT
    assert "block/<block_id>/config/<key>" in ARTIFACT_MANAGER_SYSTEM_PROMPT
    assert '"op":"add_block"' in ARTIFACT_MANAGER_SYSTEM_PROMPT
    assert '"op":"remove_block"' in ARTIFACT_MANAGER_SYSTEM_PROMPT
    assert '"op":"move_block"' in ARTIFACT_MANAGER_SYSTEM_PROMPT


def test_artifact_manager_prompt_requires_reading_the_full_doc_before_complex_edits():
    assert "full current `story_doc`" in ARTIFACT_MANAGER_SYSTEM_PROMPT
    assert "preserve existing config exactly" in ARTIFACT_MANAGER_SYSTEM_PROMPT


def test_provider_neutral_artifacts_have_an_explicit_data_model_handoff():
    for prompt in (ARTIFACT_PROMPT_ADDITION, ARTIFACT_MANAGER_SYSTEM_PROMPT):
        assert 'status: "needs_data_model"' in prompt
        assert "data_requirements" in prompt
        assert "`canvas_manager`" in prompt
    assert "prepare the data model first" in ARTIFACT_PROMPT_ADDITION
    assert "Only after the user requests or approves creating/saving" in ARTIFACT_PROMPT_ADDITION
    assert "permission to change the model from a chart request alone" in ARTIFACT_PROMPT_ADDITION
    assert "examined versus\neligible rows" in ARTIFACT_PROMPT_ADDITION
    assert "keyword rules are not NLP" in ARTIFACT_PROMPT_ADDITION
    assert "identity/version guard" in ARTIFACT_PROMPT_ADDITION
    for prompt in (ARTIFACT_PROMPT_ADDITION, ARTIFACT_MANAGER_SYSTEM_PROMPT):
        assert "OCS" not in prompt
        prose = prompt.split("Data requirements JSON Schema:")[0]
        assert "grain" in prose
        assert "provider\nname" in prompt or "provider name" in prompt
    assert "dimension, measure, dataset, or relationship" in ARTIFACT_MANAGER_SYSTEM_PROMPT
    assert "reports the same `needs_data_model` gap again" in ARTIFACT_PROMPT_ADDITION
    assert "Do not invent a\n  taxonomy from column names" in CANVAS_MANAGER_SYSTEM_PROMPT
    assert {"list_datasets", "describe_dataset", "semantic_query"} == NESTED_MCP_TOOL_NAMES


@pytest.mark.parametrize(
    "prompt",
    [
        BASE_SYSTEM_PROMPT,
        HEADLESS_BASE_SYSTEM_PROMPT,
        READ_ONLY_BASE_SYSTEM_PROMPT,
        ARTIFACT_PROMPT_ADDITION,
        ARTIFACT_MANAGER_SYSTEM_PROMPT,
    ],
    ids=["interactive", "headless", "read_only", "artifact_parent", "artifact_manager"],
)
def test_missing_member_guidance_checks_catalog_before_model_changes(prompt):
    prose = " ".join(prompt.split())
    assert "`list_datasets` / `describe_dataset`" in prose
    assert "existing member satisfies the requested meaning" in prose
    assert "validate again without changing the model" in prose
    assert "neither a typo nor a missing capability" in prose
    assert "never substitute a similarly named member with different semantics" in prose
    assert "Only a confirmed capability gap" in prose


@pytest.mark.parametrize(
    "prompt",
    [
        BASE_SYSTEM_PROMPT,
        HEADLESS_BASE_SYSTEM_PROMPT,
        READ_ONLY_BASE_SYSTEM_PROMPT,
        ARTIFACT_MANAGER_SYSTEM_PROMPT,
    ],
)
def test_unknown_repair_is_not_presented_as_authorized_recovery(prompt):
    prose = " ".join(prompt.split())
    assert "repair could not be determined" in prose
    assert "diagnostics" in prose
    assert "do not guess" in prose
