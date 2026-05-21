"""Module-level constants for the chat app shared across modules."""

# Marker prefix on synthetic HumanMessages injected by the materialization
# resume task. Hidden from the UI render in langchain_messages_to_ui so the
# user only sees the agent's response, not the system-framed kick.
SYSTEM_RESUME_MARKER = "[__system_resume__]"
