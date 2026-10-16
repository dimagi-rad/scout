"""Module-level constants for the chat app shared across modules."""

# Marker prefix on synthetic HumanMessages injected by the materialization resume
# task; hidden in langchain_messages_to_ui so the user only sees the agent's response.
SYSTEM_RESUME_MARKER = "[__system_resume__]"

MAX_MESSAGE_LENGTH = 10_000

# Shown when a turn hits its recursion limit. The work so far is checkpointed, so
# a follow-up continues from it.
STEP_LIMIT_NOTICE = (
    "I ran out of steps before finishing this answer. "
    "Ask me to continue and I'll carry on from where I stopped."
)
