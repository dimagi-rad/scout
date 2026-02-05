"""
Main Chainlit entrypoint for the Scout data agent platform.

This module provides the web-based chat interface for interacting with
the Scout data agent. It handles:
- User authentication (password-based for development)
- Project selection via chat settings
- LangGraph agent initialization and management
- Message routing and streaming responses
- Artifact rendering for visualization results

Usage:
    chainlit run chainlit_app/app.py --port 8000
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING, Any

import chainlit as cl
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

# Setup Django before importing Django models
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

import django

django.setup()

from apps.agents.graph.base import build_agent_graph
from apps.projects.models import Project, ProjectMembership, ProjectRole

from chainlit_app.artifacts import handle_artifact_message, render_artifact_iframe
from chainlit_app.auth import get_django_user, password_auth_callback

if TYPE_CHECKING:
    from apps.users.models import User

logger = logging.getLogger(__name__)

# In-memory checkpointer for MVP - stores conversation state
# In production, replace with PostgresSaver for persistence
memory_checkpointer = MemorySaver()


@cl.password_auth_callback
async def auth_callback(username: str, password: str) -> cl.User | None:
    """
    Password authentication callback for Chainlit.

    Delegates to the auth module's password_auth_callback which validates
    credentials against the Django User model.
    """
    return await password_auth_callback(username, password)


@cl.on_chat_start
async def on_chat_start() -> None:
    """
    Initialize the chat session when a user connects.

    This handler:
    1. Loads the user's available projects via ProjectMembership
    2. Displays a project selector using ChatSettings
    3. Initializes the session with a unique thread ID
    """
    user = cl.user_session.get("user")
    if not user:
        await cl.Message(
            content="Authentication required. Please log in to continue.",
            author="system",
        ).send()
        return

    # Get the Django user
    django_user = get_django_user(user)
    if not django_user:
        await cl.Message(
            content="User account not found. Please contact an administrator.",
            author="system",
        ).send()
        return

    # Store Django user info in session
    cl.user_session.set("django_user_id", str(django_user.id))
    cl.user_session.set("django_user_email", django_user.email)

    # Load user's projects
    memberships = ProjectMembership.objects.filter(
        user=django_user
    ).select_related("project").order_by("project__name")

    if not memberships.exists():
        await cl.Message(
            content=(
                "You don't have access to any projects yet. "
                "Please contact an administrator to get access."
            ),
            author="system",
        ).send()
        return

    # Build project choices for the settings selector
    project_choices = {
        str(m.project.id): f"{m.project.name} ({m.role})"
        for m in memberships
    }

    # Store project memberships for later lookup
    cl.user_session.set("project_memberships", {
        str(m.project.id): {
            "project_id": str(m.project.id),
            "project_name": m.project.name,
            "project_slug": m.project.slug,
            "role": m.role,
        }
        for m in memberships
    })

    # Create chat settings with project selector
    settings = await cl.ChatSettings(
        [
            cl.input_widget.Select(
                id="project_id",
                label="Select Project",
                values=list(project_choices.keys()),
                initial_value=list(project_choices.keys())[0] if project_choices else None,
            ),
        ]
    ).send()

    # Initialize with the first project
    if project_choices:
        first_project_id = list(project_choices.keys())[0]
        await setup_project(first_project_id)

    # Generate a unique thread ID for this conversation
    thread_id = str(uuid.uuid4())
    cl.user_session.set("thread_id", thread_id)

    # Send welcome message
    project_info = cl.user_session.get("current_project")
    if project_info:
        await cl.Message(
            content=(
                f"Welcome! You're connected to **{project_info['project_name']}**.\n\n"
                "Ask me questions about your data, and I'll help you explore it. "
                "You can switch projects using the settings panel."
            ),
            author="assistant",
        ).send()


@cl.on_settings_update
async def on_settings_update(settings: dict) -> None:
    """
    Handle project selection changes from the settings panel.

    When the user selects a different project, this handler:
    1. Updates the session with the new project
    2. Rebuilds the LangGraph agent for the new project
    3. Resets the conversation thread
    """
    project_id = settings.get("project_id")
    if not project_id:
        return

    # Check if project actually changed
    current_project = cl.user_session.get("current_project")
    if current_project and current_project.get("project_id") == project_id:
        return

    await setup_project(project_id)

    # Generate new thread ID for the new project context
    thread_id = str(uuid.uuid4())
    cl.user_session.set("thread_id", thread_id)

    project_info = cl.user_session.get("current_project")
    await cl.Message(
        content=(
            f"Switched to **{project_info['project_name']}**. "
            "Starting a new conversation."
        ),
        author="system",
    ).send()


async def setup_project(project_id: str) -> bool:
    """
    Set up the agent for the selected project.

    This function:
    1. Validates the user has access to the project
    2. Loads the Project model from the database
    3. Builds the LangGraph agent with project-specific tools
    4. Stores the agent in the user session

    Args:
        project_id: UUID of the project to set up.

    Returns:
        True if setup succeeded, False otherwise.
    """
    memberships = cl.user_session.get("project_memberships", {})
    membership_info = memberships.get(project_id)

    if not membership_info:
        await cl.Message(
            content="You don't have access to this project.",
            author="system",
        ).send()
        return False

    try:
        # Load the project from the database
        project = Project.objects.get(pk=project_id)
    except Project.DoesNotExist:
        await cl.Message(
            content="Project not found. It may have been deleted.",
            author="system",
        ).send()
        return False

    # Get Django user for the agent
    django_user_id = cl.user_session.get("django_user_id")
    django_user = None
    if django_user_id:
        from apps.users.models import User
        try:
            django_user = User.objects.get(pk=django_user_id)
        except User.DoesNotExist:
            pass

    # Build the LangGraph agent for this project
    logger.info("Building agent for project: %s", project.slug)

    try:
        agent = build_agent_graph(
            project=project,
            user=django_user,
            checkpointer=memory_checkpointer,
        )
    except Exception as e:
        logger.exception("Failed to build agent for project %s: %s", project.slug, e)
        await cl.Message(
            content="Failed to initialize the agent. Please try again or contact support.",
            author="system",
        ).send()
        return False

    # Store project info and agent in session
    cl.user_session.set("current_project", {
        "project_id": str(project.id),
        "project_name": project.name,
        "project_slug": project.slug,
        "role": membership_info["role"],
    })
    cl.user_session.set("agent", agent)

    logger.info(
        "Agent initialized for project %s (user role: %s)",
        project.slug,
        membership_info["role"],
    )
    return True


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """
    Handle incoming user messages.

    This handler:
    1. Validates the session has an active agent
    2. Invokes the LangGraph agent with the user's message
    3. Streams the response back to the user
    4. Handles tool calls and artifact rendering
    """
    agent = cl.user_session.get("agent")
    if not agent:
        await cl.Message(
            content="No project selected. Please select a project from the settings.",
            author="system",
        ).send()
        return

    project_info = cl.user_session.get("current_project")
    if not project_info:
        await cl.Message(
            content="Session error. Please refresh the page.",
            author="system",
        ).send()
        return

    thread_id = cl.user_session.get("thread_id")
    if not thread_id:
        thread_id = str(uuid.uuid4())
        cl.user_session.set("thread_id", thread_id)

    # Build the initial state for the agent
    django_user_id = cl.user_session.get("django_user_id", "")

    input_state = {
        "messages": [HumanMessage(content=message.content)],
        "project_id": project_info["project_id"],
        "project_name": project_info["project_name"],
        "user_id": django_user_id,
        "user_role": project_info["role"],
        "needs_correction": False,
        "retry_count": 0,
        "correction_context": {},
    }

    config = {"configurable": {"thread_id": thread_id}}

    # Create a message placeholder for streaming
    response_message = cl.Message(content="", author="assistant")
    await response_message.send()

    try:
        # Stream the agent's response
        collected_content = ""
        tool_calls_processed = set()

        async for event in agent.astream_events(input_state, config=config, version="v2"):
            event_type = event.get("event")

            # Handle streaming tokens from the LLM
            if event_type == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk and hasattr(chunk, "content") and chunk.content:
                    # Handle string content
                    if isinstance(chunk.content, str):
                        collected_content += chunk.content
                        await response_message.stream_token(chunk.content)
                    # Handle list content (e.g., content blocks)
                    elif isinstance(chunk.content, list):
                        for block in chunk.content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "")
                                collected_content += text
                                await response_message.stream_token(text)
                            elif hasattr(block, "text"):
                                collected_content += block.text
                                await response_message.stream_token(block.text)

            # Handle tool call results
            elif event_type == "on_tool_end":
                tool_output = event.get("data", {}).get("output")
                run_id = event.get("run_id")

                # Avoid processing the same tool call twice
                if run_id and run_id in tool_calls_processed:
                    continue
                if run_id:
                    tool_calls_processed.add(run_id)

                if tool_output:
                    await process_tool_output(tool_output, response_message)

        # Finalize the response message
        response_message.content = collected_content
        await response_message.update()

    except Exception as e:
        logger.exception("Error during agent invocation: %s", e)
        await cl.Message(
            content=f"An error occurred while processing your request: {e!s}",
            author="system",
        ).send()


async def process_tool_output(tool_output: Any, response_message: cl.Message) -> None:
    """
    Process tool output and handle special cases like artifacts.

    Args:
        tool_output: The output from a tool execution.
        response_message: The current response message to update.
    """
    # Convert to string if needed
    if isinstance(tool_output, ToolMessage):
        content = tool_output.content
    elif isinstance(tool_output, str):
        content = tool_output
    else:
        content = str(tool_output)

    # Check for artifact creation in the output
    if _is_artifact_result(content):
        artifact_info = _extract_artifact_from_result(content)
        if artifact_info:
            # Render the artifact iframe
            iframe_html = render_artifact_iframe(
                artifact_id=artifact_info["artifact_id"],
                artifact_type=artifact_info.get("type", "chart"),
                title=artifact_info.get("title"),
            )

            # Send artifact as a separate message
            await cl.Message(
                content=iframe_html,
                author="system",
            ).send()


def _is_artifact_result(content: str) -> bool:
    """Check if the content appears to be an artifact creation result."""
    if not content:
        return False

    artifact_indicators = [
        "artifact_id",
        "artifact created",
        "created artifact",
        "chart saved",
        "visualization created",
    ]

    content_lower = content.lower()
    return any(indicator in content_lower for indicator in artifact_indicators)


def _extract_artifact_from_result(content: str) -> dict | None:
    """Extract artifact information from a tool result."""
    import json
    import re

    # Try JSON parsing
    try:
        if "{" in content:
            # Find JSON-like content
            json_match = re.search(r"\{[^{}]*\}", content)
            if json_match:
                data = json.loads(json_match.group())
                if "artifact_id" in data:
                    return data
    except (json.JSONDecodeError, AttributeError):
        pass

    # Try regex for UUID
    uuid_pattern = r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    match = re.search(uuid_pattern, content, re.IGNORECASE)
    if match:
        return {"artifact_id": match.group(1)}

    return None


# Entry point for running with `python -m chainlit_app.app`
if __name__ == "__main__":
    from chainlit.cli import run_chainlit

    run_chainlit(__file__)
