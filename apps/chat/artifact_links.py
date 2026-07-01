"""Thread/artifact relationship helpers."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from apps.artifacts.models import Artifact
from apps.chat.models import Thread, ThreadArtifact
from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)

_ARTIFACT_ID_KEYS = frozenset({"artifact_id", "previous_artifact_id", "previous_version_id"})


def _clean_thread_id(thread_id: str | None) -> str | None:
    if not thread_id:
        return None
    try:
        return str(UUID(str(thread_id)))
    except (TypeError, ValueError):
        return None


def _source_value(source: ThreadArtifact.Source | str) -> str:
    return source.value if isinstance(source, ThreadArtifact.Source) else str(source)


def _source_from_status(value: dict[str, Any]) -> str:
    status = str(value.get("status") or "").lower()
    if status == "created":
        return ThreadArtifact.Source.CREATED
    if status == "updated":
        return ThreadArtifact.Source.UPDATED
    return ThreadArtifact.Source.MENTIONED


def _merge_reference(
    references: dict[str, str],
    artifact_id: Any,
    source: ThreadArtifact.Source | str,
) -> None:
    clean_artifact_id = _clean_thread_id(str(artifact_id) if artifact_id else None)
    if clean_artifact_id is None:
        return
    current = references.get(clean_artifact_id)
    next_source = _source_value(source)
    if current == ThreadArtifact.Source.CREATED:
        return
    if current == ThreadArtifact.Source.UPDATED and next_source == ThreadArtifact.Source.MENTIONED:
        return
    references[clean_artifact_id] = next_source


def _extract_artifact_references(value: Any) -> dict[str, str]:
    """Extract explicit artifact references from saved UI/tool payloads."""

    references: dict[str, str] = {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return references
        return _extract_artifact_references(parsed)

    if isinstance(value, list):
        for item in value:
            for artifact_id, source in _extract_artifact_references(item).items():
                _merge_reference(references, artifact_id, source)
        return references

    if not isinstance(value, dict):
        return references

    source = _source_from_status(value)
    for key, child in value.items():
        normalized_key = str(key).lower()
        if normalized_key == "artifact" and isinstance(child, dict):
            _merge_reference(references, child.get("id"), source)
        elif normalized_key in _ARTIFACT_ID_KEYS:
            child_source = (
                ThreadArtifact.Source.MENTIONED
                if normalized_key.startswith("previous")
                else source
            )
            _merge_reference(references, child, child_source)

        for artifact_id, child_source in _extract_artifact_references(child).items():
            _merge_reference(references, artifact_id, child_source)

    return references


async def _load_thread_ui_messages(thread_id: str) -> list[dict[str, Any]]:
    from apps.chat.checkpointer import ensure_checkpointer
    from apps.chat.message_converter import langchain_messages_to_ui

    checkpointer = await ensure_checkpointer()
    checkpoint_tuple = await checkpointer.aget_tuple(
        {"configurable": {"thread_id": str(thread_id)}}
    )
    if checkpoint_tuple is None:
        return []
    lc_messages = (
        (checkpoint_tuple.checkpoint or {})
        .get("channel_values", {})
        .get("messages", [])
    )
    return langchain_messages_to_ui(lc_messages)


async def link_artifact_to_thread(
    artifact: Artifact,
    thread_id: str | None,
    workspace: Workspace,
    *,
    source: ThreadArtifact.Source | str,
    message_id: str = "",
    tool_call_id: str = "",
) -> ThreadArtifact | None:
    """Attach an artifact version to a thread if that thread exists in the workspace."""

    clean_thread_id = _clean_thread_id(thread_id)
    if clean_thread_id is None:
        return None

    thread = await Thread.objects.filter(id=clean_thread_id, workspace=workspace).afirst()
    if thread is None:
        return None

    defaults = {
        "workspace": workspace,
        "source": _source_value(source),
        "message_id": message_id[:128],
        "tool_call_id": tool_call_id[:128],
    }
    link, _ = await ThreadArtifact.objects.aupdate_or_create(
        thread=thread,
        artifact=artifact,
        defaults=defaults,
    )
    return link


async def backfill_thread_artifact_links(thread: Thread) -> int:
    """Create links for legacy and saved-message artifact references."""

    created = 0
    queryset = Artifact.objects.filter(
        workspace_id=thread.workspace_id,
        conversation_id=str(thread.id),
    )
    async for artifact in queryset:
        _, was_created = await ThreadArtifact.objects.aget_or_create(
            thread=thread,
            artifact=artifact,
            defaults={
                "workspace_id": thread.workspace_id,
                "source": (
                    ThreadArtifact.Source.UPDATED
                    if artifact.parent_artifact_id
                    else ThreadArtifact.Source.CREATED
                ),
            },
        )
        if was_created:
            created += 1

    try:
        messages = await _load_thread_ui_messages(str(thread.id))
    except Exception:
        logger.info("Could not inspect thread %s messages for artifact links", thread.id, exc_info=True)
        return created

    references: dict[str, str] = {}
    for message in messages:
        for artifact_id, source in _extract_artifact_references(message).items():
            _merge_reference(references, artifact_id, source)

    if not references:
        return created

    referenced_artifacts = Artifact.objects.filter(
        workspace_id=thread.workspace_id,
        id__in=references.keys(),
    )
    async for artifact in referenced_artifacts:
        _, was_created = await ThreadArtifact.objects.aget_or_create(
            thread=thread,
            artifact=artifact,
            defaults={
                "workspace_id": thread.workspace_id,
                "source": references[str(artifact.id)],
            },
        )
        if was_created:
            created += 1
    return created


def serialize_thread_artifact_link(link: ThreadArtifact) -> dict:
    artifact = link.artifact
    return {
        "id": str(artifact.id),
        "title": artifact.title,
        "description": artifact.description,
        "artifact_type": artifact.artifact_type,
        "version": artifact.version,
        "source": link.source,
        "created_at": artifact.created_at.isoformat(),
        "updated_at": artifact.updated_at.isoformat(),
        "linked_at": link.created_at.isoformat(),
        "last_seen_at": link.last_seen_at.isoformat(),
    }
