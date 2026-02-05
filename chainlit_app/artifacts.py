"""
Artifact rendering helpers for the Scout Chainlit application.

Provides utilities for rendering artifacts (charts, tables, reports) as
interactive iframes within the chat interface.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import chainlit as cl

if TYPE_CHECKING:
    from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

# Base URL for artifact sandbox (configurable via environment)
ARTIFACT_SANDBOX_BASE_URL = "/artifacts/{artifact_id}/sandbox"


def render_artifact_iframe(
    artifact_id: str,
    artifact_type: str = "chart",
    title: str | None = None,
    height: int = 400,
) -> str:
    """
    Create an iframe HTML snippet for rendering an artifact.

    The iframe points to the artifact's sandbox endpoint which serves
    the artifact content in an isolated context for security.

    Args:
        artifact_id: UUID of the artifact to render.
        artifact_type: Type of artifact (chart, table, report). Used for sizing.
        title: Optional title to display above the iframe.
        height: Height of the iframe in pixels. Defaults vary by type.

    Returns:
        HTML string containing the iframe element.
    """
    # Adjust height based on artifact type
    type_heights = {
        "chart": 400,
        "table": 300,
        "report": 600,
    }
    effective_height = height or type_heights.get(artifact_type, 400)

    # Build the sandbox URL
    sandbox_url = ARTIFACT_SANDBOX_BASE_URL.format(artifact_id=artifact_id)

    # Construct the iframe HTML
    title_html = f"<p><strong>{title}</strong></p>" if title else ""

    iframe_html = f"""
{title_html}
<iframe
    src="{sandbox_url}"
    width="100%"
    height="{effective_height}px"
    style="border: 1px solid #e0e0e0; border-radius: 8px;"
    sandbox="allow-scripts allow-same-origin"
    loading="lazy"
></iframe>
"""
    return iframe_html.strip()


async def handle_artifact_message(tool_message: "ToolMessage") -> cl.Message | None:
    """
    Process an artifact tool result and render it in the chat.

    Extracts artifact information from the tool message content and
    creates an appropriate Chainlit message with the rendered artifact.

    Args:
        tool_message: The ToolMessage containing artifact creation result.

    Returns:
        A Chainlit Message with the rendered artifact, or None if not an artifact.
    """
    content = tool_message.content

    # Check if this is an artifact creation result
    # Expected format: JSON or structured text with artifact_id
    if not content:
        return None

    # Try to parse as structured artifact result
    artifact_info = _extract_artifact_info(content)

    if not artifact_info:
        return None

    artifact_id = artifact_info.get("artifact_id")
    artifact_type = artifact_info.get("type", "chart")
    title = artifact_info.get("title")

    if not artifact_id:
        logger.warning("Artifact message missing artifact_id: %s", content)
        return None

    # Render the artifact iframe
    iframe_html = render_artifact_iframe(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        title=title,
    )

    # Create and return the Chainlit message
    return cl.Message(
        content=iframe_html,
        author="system",
    )


def _extract_artifact_info(content: str) -> dict | None:
    """
    Extract artifact information from tool message content.

    Supports multiple formats:
    - JSON: {"artifact_id": "...", "type": "...", "title": "..."}
    - Text: "Created artifact: <id>" or "Artifact ID: <id>"

    Args:
        content: The tool message content to parse.

    Returns:
        Dictionary with artifact info, or None if not parseable.
    """
    import json

    # Try JSON parsing first
    try:
        if content.strip().startswith("{"):
            data = json.loads(content)
            if "artifact_id" in data:
                return data
    except json.JSONDecodeError:
        pass

    # Try regex patterns for common formats
    patterns = [
        # UUID pattern in various contexts
        r"artifact[_\s]?id[:\s]+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        r"created\s+artifact[:\s]+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        # Generic UUID at the end of a line
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    ]

    for pattern in patterns:
        match = re.search(pattern, content, re.IGNORECASE | re.MULTILINE)
        if match:
            return {"artifact_id": match.group(1)}

    return None


async def send_artifact_element(
    artifact_id: str,
    artifact_type: str = "chart",
    title: str | None = None,
    height: int = 400,
) -> None:
    """
    Send an artifact as a Chainlit element in the current message context.

    This is a convenience function for sending artifacts as part of
    the streaming response flow.

    Args:
        artifact_id: UUID of the artifact to render.
        artifact_type: Type of artifact (chart, table, report).
        title: Optional title to display above the artifact.
        height: Height of the iframe in pixels.
    """
    iframe_html = render_artifact_iframe(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        title=title,
        height=height,
    )

    # Send as a text element with HTML
    element = cl.Text(
        name=title or f"Artifact {artifact_id[:8]}",
        content=iframe_html,
        display="inline",
    )

    await cl.Message(
        content="",
        elements=[element],
    ).send()


def create_artifact_card(
    artifact_id: str,
    title: str,
    description: str | None = None,
    artifact_type: str = "chart",
) -> str:
    """
    Create an artifact card with preview and action buttons.

    Returns HTML for a card that shows artifact metadata with a link
    to view the full artifact.

    Args:
        artifact_id: UUID of the artifact.
        title: Title of the artifact.
        description: Optional description text.
        artifact_type: Type of artifact for icon selection.

    Returns:
        HTML string for the artifact card.
    """
    # Type icons
    type_icons = {
        "chart": "bar-chart-2",
        "table": "table",
        "report": "file-text",
    }
    icon = type_icons.get(artifact_type, "file")

    sandbox_url = ARTIFACT_SANDBOX_BASE_URL.format(artifact_id=artifact_id)
    description_html = f"<p style='color: #666; margin: 4px 0;'>{description}</p>" if description else ""

    card_html = f"""
<div style="border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px; margin: 8px 0; background: #fafafa;">
    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
        <span style="font-size: 20px;">{"chart" if icon == "bar-chart-2" else "table" if icon == "table" else "document"}</span>
        <strong>{title}</strong>
    </div>
    {description_html}
    <a href="{sandbox_url}" target="_blank" style="color: #0066cc; text-decoration: none;">
        View full artifact
    </a>
</div>
"""
    return card_html.strip()
