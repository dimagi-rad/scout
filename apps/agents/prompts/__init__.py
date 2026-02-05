"""
Prompt templates for Scout data agent.

This package contains system prompts, dynamic prompt builders, and
context formatters for the Scout conversation agent.
"""

from apps.agents.prompts.artifact_prompt import ARTIFACT_PROMPT_ADDITION
from apps.agents.prompts.base_system import BASE_SYSTEM_PROMPT

__all__ = [
    "BASE_SYSTEM_PROMPT",
    "ARTIFACT_PROMPT_ADDITION",
]
