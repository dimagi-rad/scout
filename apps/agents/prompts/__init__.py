"""
Prompt templates for Scout data agent.

This package contains system prompts, dynamic prompt builders, and
context formatters for the Scout conversation agent.
"""

from apps.agents.prompts.artifact_prompt import ARTIFACT_PROMPT_ADDITION
from apps.agents.prompts.base_system import (
    BASE_SYSTEM_PROMPT,
    HEADLESS_BASE_SYSTEM_PROMPT,
    READ_ONLY_BASE_SYSTEM_PROMPT,
    select_base_system_prompt,
)

__all__ = [
    "ARTIFACT_PROMPT_ADDITION",
    "BASE_SYSTEM_PROMPT",
    "HEADLESS_BASE_SYSTEM_PROMPT",
    "READ_ONLY_BASE_SYSTEM_PROMPT",
    "select_base_system_prompt",
]
