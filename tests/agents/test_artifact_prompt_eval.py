"""Prompt contract checks for semantic graph artifact creation."""

from apps.agents.prompts.artifact_prompt import ARTIFACT_PROMPT_ADDITION
from apps.agents.tools.artifact_manager_agent import ARTIFACT_MANAGER_SYSTEM_PROMPT


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
