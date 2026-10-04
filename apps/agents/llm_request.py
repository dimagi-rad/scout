"""Request options shared by every ChatAnthropic the agent and its subagents build."""

from typing import Any, Literal

import httpx
from anthropic import APITimeoutError
from django.conf import settings

THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"

# Set explicitly: Opus 5.5's API default is "medium", but an unset effort
# silently follows whatever the next model's default is. The subagents do narrow,
# well-specified edits and run at "low".
MAIN_AGENT_EFFORT = "medium"
SUBAGENT_EFFORT = "low"

# A connect/read timeout before the response starts surfaces as APITimeoutError (after
# the SDK's retries); one between chunks of an open stream escapes the SDK unwrapped.
# Treating these as model timeouts relies on no tool letting a bare httpx timeout
# escape: ToolNode re-raises tool errors as they are, and today the MCP transport
# and cube_client wrap theirs. A tool or node that does raw httpx I/O would be
# mislabelled as a slow model.
LLM_TIMEOUT_ERRORS = (APITimeoutError, httpx.TimeoutException)


def chat_model_kwargs(effort: Literal["low", "medium", "high"]) -> dict[str, Any]:
    """Fresh ChatAnthropic kwargs for thinking behaviour (a new dict per call).

    Scout edits the replayed prefix on purpose: ``prune_messages`` slides the
    history window and the system prompt's date line changes daily. Where the
    account enforces thinking-block binding, replaying a block after such an edit
    is a 400; ``drop_block`` discards the stale block and the request goes ahead.

    ``display: "summarized"`` because the default (``omitted``) returns thinking
    blocks with empty text, which leaves the chat's Thinking card blank. Opus 5.5
    also writes its between-tool-call notes as thinking blocks, so without this
    those notes never reach the user either.

    ``timeout`` because langchain-anthropic passes ``None`` by default, which turns
    off the SDK's own 600s cap and lets one hung connection stall a turn forever.
    It is a single float (langchain-anthropic rejects an ``httpx.Timeout``), so it
    bounds connect and each read alike. ``streaming`` so callers that ``ainvoke``
    without a streaming callback (recipes) stream too: a non-streamed reply sends
    no bytes until it is done, which would turn the read timeout into a cap on the
    whole generation.
    """
    return {
        "timeout": settings.LLM_REQUEST_TIMEOUT_S,
        "streaming": True,
        "thinking": {
            "type": "adaptive",
            "display": "summarized",
            "block_binding": {"prefix_mismatch_behavior": "drop_block"},
        },
        "effort": effort,
        "betas": [THINKING_BINDING_BETA],
    }
