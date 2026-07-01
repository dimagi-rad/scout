"""Prompt contract checks for semantic graph artifact creation."""

from apps.agents.prompts.artifact_prompt import ARTIFACT_PROMPT_ADDITION


def test_artifact_prompt_lists_narrative_block_config_keys():
    old_manager_name = "`artifact_" + "graph_manager`"

    assert "`artifact_manager`" in ARTIFACT_PROMPT_ADDITION
    assert old_manager_name not in ARTIFACT_PROMPT_ADDITION
    assert "Block config keys:" in ARTIFACT_PROMPT_ADDITION
    assert "call `artifact_manager` first" in ARTIFACT_PROMPT_ADDITION
    assert "Do not preflight the task" in ARTIFACT_PROMPT_ADDITION
    assert "`section`: `title`, `body`" in ARTIFACT_PROMPT_ADDITION
    assert "`tldr`: `content`" in ARTIFACT_PROMPT_ADDITION
    assert "`markdown`: `body` or `content`" in ARTIFACT_PROMPT_ADDITION
    assert "Do not use `text`" in ARTIFACT_PROMPT_ADDITION
