"""
Conversation logging service for audit and analytics.

This module provides the ConversationLogger class which records
conversation history, tool calls, SQL executed, and artifacts created.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from django.utils import timezone

if TYPE_CHECKING:
    from apps.projects.models import ConversationLog, Project
    from apps.users.models import User

logger = logging.getLogger(__name__)


class ConversationLogger:
    """
    Logs conversation history for audit and analytics.

    Records:
    - User messages and agent responses
    - Tool calls and their results
    - SQL queries executed
    - Artifacts created
    - Timing information
    """

    def __init__(
        self,
        project: Project,
        user: User,
        thread_id: str,
    ):
        """
        Initialize the conversation logger.

        Args:
            project: The project this conversation belongs to
            user: The user having the conversation
            thread_id: Unique identifier for this conversation thread
        """
        self.project = project
        self.user = user
        self.thread_id = thread_id
        self._messages: list[dict] = []
        self._queries_executed: list[dict] = []
        self._artifacts_created: list[str] = []
        self._log: ConversationLog | None = None

    def _get_or_create_log(self) -> ConversationLog:
        """Get or create the ConversationLog record."""
        if self._log is not None:
            return self._log

        from apps.projects.models import ConversationLog

        self._log, created = ConversationLog.objects.get_or_create(
            project=self.project,
            user=self.user,
            thread_id=self.thread_id,
            defaults={
                "messages": [],
                "queries_executed": [],
            },
        )

        if not created:
            # Load existing data
            self._messages = self._log.messages or []
            self._queries_executed = self._log.queries_executed or []

        return self._log

    def log_user_message(self, content: str) -> None:
        """
        Log a user message.

        Args:
            content: The message content
        """
        self._messages.append({
            "role": "user",
            "content": content,
            "timestamp": timezone.now().isoformat(),
        })
        self._save()

    def log_assistant_message(self, content: str) -> None:
        """
        Log an assistant message.

        Args:
            content: The message content
        """
        self._messages.append({
            "role": "assistant",
            "content": content,
            "timestamp": timezone.now().isoformat(),
        })
        self._save()

    def log_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_output: Any,
        duration_ms: int | None = None,
    ) -> None:
        """
        Log a tool call and its result.

        Args:
            tool_name: Name of the tool that was called
            tool_input: Input parameters to the tool
            tool_output: Output from the tool
            duration_ms: Optional execution duration in milliseconds
        """
        self._messages.append({
            "role": "tool",
            "tool_name": tool_name,
            "tool_input": self._serialize(tool_input),
            "tool_output": self._serialize(tool_output),
            "duration_ms": duration_ms,
            "timestamp": timezone.now().isoformat(),
        })
        self._save()

    def log_sql_query(
        self,
        sql: str,
        row_count: int | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        """
        Log an executed SQL query.

        Args:
            sql: The SQL query that was executed
            row_count: Number of rows returned (if successful)
            duration_ms: Execution time in milliseconds
            error: Error message if query failed
        """
        self._queries_executed.append({
            "sql": sql,
            "row_count": row_count,
            "duration_ms": duration_ms,
            "error": error,
            "timestamp": timezone.now().isoformat(),
        })
        self._save()

    def log_artifact_created(self, artifact_id: str) -> None:
        """
        Log an artifact that was created.

        Args:
            artifact_id: UUID of the created artifact
        """
        self._artifacts_created.append(artifact_id)

        # Also add to messages for context
        self._messages.append({
            "role": "system",
            "event": "artifact_created",
            "artifact_id": artifact_id,
            "timestamp": timezone.now().isoformat(),
        })
        self._save()

    def log_error(self, error: str, context: dict | None = None) -> None:
        """
        Log an error that occurred.

        Args:
            error: Error message
            context: Optional context about where the error occurred
        """
        self._messages.append({
            "role": "system",
            "event": "error",
            "error": error,
            "context": context,
            "timestamp": timezone.now().isoformat(),
        })
        self._save()

    def _serialize(self, obj: Any) -> Any:
        """Serialize an object for JSON storage."""
        if obj is None:
            return None

        if isinstance(obj, (str, int, float, bool)):
            return obj

        if isinstance(obj, (list, tuple)):
            return [self._serialize(item) for item in obj]

        if isinstance(obj, dict):
            return {str(k): self._serialize(v) for k, v in obj.items()}

        if isinstance(obj, datetime):
            return obj.isoformat()

        # For other objects, try to convert to string
        try:
            return str(obj)
        except Exception:
            return "<unserializable>"

    def _save(self) -> None:
        """Save the conversation log to the database."""
        log = self._get_or_create_log()
        log.messages = self._messages
        log.queries_executed = self._queries_executed
        log.save(update_fields=["messages", "queries_executed", "updated_at"])

    def get_summary(self) -> dict:
        """
        Get a summary of the conversation.

        Returns:
            Dict with conversation statistics
        """
        user_messages = sum(1 for m in self._messages if m.get("role") == "user")
        assistant_messages = sum(1 for m in self._messages if m.get("role") == "assistant")
        tool_calls = sum(1 for m in self._messages if m.get("role") == "tool")
        errors = sum(1 for m in self._messages if m.get("event") == "error")

        return {
            "thread_id": self.thread_id,
            "user_messages": user_messages,
            "assistant_messages": assistant_messages,
            "tool_calls": tool_calls,
            "queries_executed": len(self._queries_executed),
            "artifacts_created": len(self._artifacts_created),
            "errors": errors,
            "total_messages": len(self._messages),
        }


__all__ = ["ConversationLogger"]
