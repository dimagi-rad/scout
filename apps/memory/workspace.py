"""Workspace memory (#849): shared notes about a workspace's data, stored as AgentLearning.

Every member can read them; READ_WRITE and MANAGE members can add them; only the
author (while still able to write) or a manager can edit or delete one. Every
change is recorded as a WorkspaceMemoryEvent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Count, Max, Q

from apps.knowledge.models import AgentLearning
from apps.knowledge.services.retriever import (
    LEARNINGS_CHAR_CAP,
    WORKSPACE_MEMORY_ORDER,
    KnowledgeRetriever,
    format_workspace_memories,
)
from apps.memory.models import WorkspaceMemoryEvent
from apps.memory.services import (
    MAX_WORKSPACE_MEMORY_CHARS,
    MemoryLimitReached,
    MemoryValidationError,
    normalize_memory,
)
from apps.workspaces.access import role_satisfies
from apps.workspaces.models import Workspace, WorkspaceRole

audit_logger = logging.getLogger("scout.memory.audit")

# Matches how many the retriever injects, so no active memory is left out.
MAX_WORKSPACE_MEMORIES = KnowledgeRetriever.MAX_AGENT_LEARNINGS
MAX_TABLES = 20

Source = WorkspaceMemoryEvent.Source
Action = WorkspaceMemoryEvent.Action


@dataclass(frozen=True)
class WorkspaceSaveResult:
    memory: AgentLearning
    created: bool


def can_add(role: str | None) -> bool:
    return role is not None and role_satisfies(role, WorkspaceRole.READ_WRITE)


def can_change(role: str | None, memory: AgentLearning, user) -> bool:
    """Managers may change any memory; a writer only the ones they added."""
    if role is None:
        return False
    if role_satisfies(role, WorkspaceRole.MANAGE):
        return True
    return (
        can_add(role)
        and memory.discovered_by_user_id is not None
        and memory.discovered_by_user_id == user.pk
    )


def clean_tables(tables) -> list[str]:
    if tables is None:
        return []
    if not isinstance(tables, list) or not all(isinstance(t, str) for t in tables):
        raise MemoryValidationError("tables must be a list of table names.")
    cleaned = list(dict.fromkeys(t.strip() for t in tables if t.strip()))
    if len(cleaned) > MAX_TABLES:
        raise MemoryValidationError(f"A memory can name at most {MAX_TABLES} tables.")
    return cleaned


def _audit(event: WorkspaceMemoryEvent) -> None:
    audit_logger.info(
        "workspace memory %s: workspace=%s memory=%s actor=%s source=%s",
        event.action,
        event.workspace_id,
        event.memory_id,
        event.actor_id,
        event.source,
    )


def _lock_workspace(workspace_id) -> None:
    # Serializes a workspace's memory writes so concurrent saves can't both pass
    # the duplicate and limit checks.
    Workspace.objects.select_for_update().filter(pk=workspace_id).first()


def _duplicate(workspace_id, content: str, *, excluding=None) -> AgentLearning | None:
    rows = AgentLearning.objects.filter(
        workspace_id=workspace_id, is_active=True, description__iexact=content
    )
    if excluding is not None:
        rows = rows.exclude(pk=excluding.pk)
    return rows.first()


def _check_fits(workspace_id, candidate: AgentLearning, *, replacing=None) -> None:
    """Refuse a write that would push any active memory out of the prompt."""
    others = AgentLearning.objects.filter(workspace_id=workspace_id, is_active=True)
    if replacing is not None:
        others = others.exclude(pk=replacing)
    rows = [*others.order_by(*WORKSPACE_MEMORY_ORDER), candidate]
    if replacing is None and len(rows) > MAX_WORKSPACE_MEMORIES:
        raise MemoryLimitReached(
            f"This workspace already has {MAX_WORKSPACE_MEMORIES} memories. "
            "A manager can delete some on the Memory page."
        )
    if len(format_workspace_memories(rows)) > LEARNINGS_CHAR_CAP:
        raise MemoryLimitReached(
            "This workspace's memory is full. Shorten or delete some memories on the "
            "Memory page (a manager can delete any of them) before adding more."
        )


@sync_to_async
def _create(workspace, user, content: str, tables: list[str], source: str) -> WorkspaceSaveResult:
    with transaction.atomic():
        _lock_workspace(workspace.pk)
        existing = _duplicate(workspace.pk, content)
        if existing is not None:
            return WorkspaceSaveResult(existing, created=False)
        _check_fits(
            workspace.pk,
            AgentLearning(description=content, applies_to_tables=tables, confidence_score=0.5),
        )
        memory = AgentLearning.objects.create(
            workspace=workspace,
            description=content,
            category="other",
            applies_to_tables=tables,
            discovered_by_user=user,
        )
        event = WorkspaceMemoryEvent.objects.create(
            workspace=workspace,
            memory_id=memory.id,
            actor=user,
            action=Action.CREATED,
            source=source,
            content=content,
        )
    _audit(event)
    return WorkspaceSaveResult(memory, created=True)


async def asave_workspace_memory(
    workspace, user, text: str, tables=None, *, source: str
) -> WorkspaceSaveResult:
    """Add a memory, or return the active one that already says the same thing.

    The caller checks the role; this only validates and writes.
    """
    content = normalize_memory(text, MAX_WORKSPACE_MEMORY_CHARS)
    return await _create(workspace, user, content, clean_tables(tables), source)


@sync_to_async
def _update(memory: AgentLearning, user, content: str, tables: list[str] | None) -> AgentLearning:
    """Raises AgentLearning.DoesNotExist if the memory was deleted meanwhile."""
    with transaction.atomic():
        _lock_workspace(memory.workspace_id)
        locked = AgentLearning.objects.select_for_update().get(pk=memory.pk)
        if _duplicate(locked.workspace_id, content, excluding=locked) is not None:
            raise MemoryValidationError("This workspace already has a memory that says this.")
        previous = locked.description
        locked.description = content
        fields = ["description", "updated_at"]
        if tables is not None:
            locked.applies_to_tables = tables
            fields.append("applies_to_tables")
        _check_fits(locked.workspace_id, locked, replacing=locked.pk)
        locked.save(update_fields=fields)
        event = WorkspaceMemoryEvent.objects.create(
            workspace_id=locked.workspace_id,
            memory_id=locked.id,
            actor=user,
            action=Action.UPDATED,
            source=Source.MEMORY_PAGE,
            content=content,
            previous_content=previous,
        )
    _audit(event)
    return locked


async def aupdate_workspace_memory(memory: AgentLearning, user, text: str, tables=None):
    content = normalize_memory(text, MAX_WORKSPACE_MEMORY_CHARS)
    cleaned = None if tables is None else clean_tables(tables)
    return await _update(memory, user, content, cleaned)


@sync_to_async
def _delete(memory: AgentLearning, user) -> None:
    with transaction.atomic():
        if not AgentLearning.objects.filter(pk=memory.pk).delete()[0]:
            return
        event = WorkspaceMemoryEvent.objects.create(
            workspace_id=memory.workspace_id,
            memory_id=memory.id,
            actor=user,
            action=Action.DELETED,
            source=Source.MEMORY_PAGE,
            content=memory.description,
        )
    _audit(event)


async def adelete_workspace_memory(memory: AgentLearning, user) -> None:
    await _delete(memory, user)


async def aworkspace_memory_fingerprint(workspace_id) -> str:
    """Changes whenever a memory is added, edited or deleted, for the prompt cache key."""
    stats = await AgentLearning.objects.filter(workspace_id=workspace_id).aaggregate(
        count=Count("id"), active=Count("id", filter=Q(is_active=True)), latest=Max("updated_at")
    )
    latest = stats["latest"].isoformat() if stats["latest"] else "-"
    return f"{stats['count']}:{stats['active']}@{latest}"
