"""Request options shared by every ChatAnthropic the agent and its subagents build."""

from typing import Any

THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"


def chat_model_kwargs() -> dict[str, Any]:
    """Fresh ChatAnthropic kwargs for thinking behaviour (a new dict per call).

    Scout edits the replayed prefix on purpose: ``prune_messages`` slides the
    history window and the system prompt's date line changes daily. Where the
    account enforces thinking-block binding, replaying a block after such an edit
    is a 400; ``drop_block`` discards the stale block and the request goes ahead.

    ``display: "summarized"`` because the default (``omitted``) returns thinking
    blocks with empty text, which leaves the chat's Thinking card blank. Opus 5.5
    also writes its between-tool-call notes as thinking blocks, so without this
    those notes never reach the user either.
    """
    return {
        "thinking": {
            "type": "adaptive",
            "display": "summarized",
            "block_binding": {"prefix_mismatch_behavior": "drop_block"},
        },
        "betas": [THINKING_BINDING_BETA],
    }
