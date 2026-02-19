"""
Recipe Runner service for the Scout data agent platform.

Executes a recipe by rendering its prompt template with variable values,
sending it to the agent, and collecting results.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from django.utils import timezone
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from apps.agents.graph.base import build_agent_graph
from apps.recipes.models import Recipe, RecipeRun, RecipeRunStatus

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

    from apps.users.models import User

logger = logging.getLogger(__name__)


class RecipeRunnerError(Exception):
    """Base exception for recipe runner errors."""

    pass


class VariableValidationError(RecipeRunnerError):
    """Raised when variable validation fails."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"Variable validation failed: {', '.join(errors)}")


class StepExecutionError(RecipeRunnerError):
    """Raised when execution fails."""

    def __init__(self, message: str) -> None:
        super().__init__(f"Execution failed: {message}")


class RecipeRunner:
    """
    Executes a recipe by sending its rendered prompt to the agent.

    The runner:
    1. Validates that all required variables are provided
    2. Creates a RecipeRun record to track execution
    3. Renders the prompt template with variable values
    4. Sends the prompt to the agent and captures results
    5. Updates the RecipeRun with results and final status
    """

    def __init__(
        self,
        recipe: Recipe,
        variable_values: dict[str, Any],
        user: User,
        graph: CompiledStateGraph | None = None,
    ) -> None:
        self.recipe = recipe
        self.variable_values = variable_values.copy()
        self.user = user
        self._provided_graph = graph
        self._graph: CompiledStateGraph | None = None
        self._run: RecipeRun | None = None
        self._thread_id: str = ""

    def validate_variables(self) -> None:
        """Validate that all required variables are provided."""
        errors = self.recipe.validate_variable_values(self.variable_values)

        if errors:
            logger.warning(
                "Variable validation failed for recipe %s: %s",
                self.recipe.name,
                errors,
            )
            raise VariableValidationError(errors)

        # Apply defaults for optional variables not provided
        for var_def in self.recipe.variables:
            var_name = var_def.get("name")
            if var_name and var_name not in self.variable_values:
                if "default" in var_def:
                    self.variable_values[var_name] = var_def["default"]

    def _build_graph(self) -> CompiledStateGraph:
        """Build or return the agent graph for execution."""
        if self._provided_graph is not None:
            return self._provided_graph

        if self._graph is None:
            self._graph = build_agent_graph(
                project=self.recipe.project,
                user=self.user,
                checkpointer=None,
            )

        return self._graph

    def _create_run_record(self) -> RecipeRun:
        """Create a RecipeRun record to track execution."""
        self._thread_id = f"recipe-run-{uuid.uuid4()}"

        run = RecipeRun.objects.create(
            recipe=self.recipe,
            status=RecipeRunStatus.RUNNING,
            variable_values=self.variable_values,
            step_results=[],
            started_at=timezone.now(),
            run_by=self.user,
        )

        logger.info(
            "Created recipe run %s for recipe %s (thread_id: %s)",
            run.id,
            self.recipe.name,
            self._thread_id,
        )

        return run

    def _extract_tools_used(self, messages: list) -> list[str]:
        """Extract tool names from agent response messages."""
        tools_used = []

        for msg in messages:
            if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls"):
                for tool_call in msg.tool_calls or []:
                    tool_name = tool_call.get("name", "")
                    if tool_name and tool_name not in tools_used:
                        tools_used.append(tool_name)

        return tools_used

    def _extract_response_content(self, messages: list) -> str:
        """Extract the final response content from agent messages."""
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content:
                if hasattr(msg, "tool_calls") and msg.tool_calls and not msg.content.strip():
                    continue
                return str(msg.content)

        return ""

    def _extract_artifacts_created(self, messages: list) -> list[str]:
        """Extract artifact IDs from tool results in the response."""
        artifact_ids = []

        for msg in messages:
            if isinstance(msg, ToolMessage):
                if msg.name in ("create_artifact", "update_artifact"):
                    try:
                        import json
                        content = msg.content
                        if isinstance(content, str):
                            result = json.loads(content)
                            if isinstance(result, dict) and "artifact_id" in result:
                                artifact_ids.append(result["artifact_id"])
                    except (json.JSONDecodeError, TypeError):
                        pass

        return artifact_ids

    def execute(self) -> RecipeRun:
        """Execute the recipe and return the RecipeRun record."""
        self.validate_variables()

        self._run = self._create_run_record()

        graph = self._build_graph()
        config = {"configurable": {"thread_id": self._thread_id}}

        # Render the prompt
        prompt = self.recipe.render_prompt(self.variable_values)

        logger.info("Starting recipe execution: %s", self.recipe.name)

        step_started = timezone.now()

        result = {
            "step_order": 1,
            "prompt": prompt,
            "response": "",
            "tools_used": [],
            "artifacts_created": [],
            "success": False,
            "error": None,
            "started_at": step_started.isoformat(),
            "completed_at": None,
        }

        try:
            initial_state = {
                "messages": [HumanMessage(content=prompt)],
                "project_id": str(self.recipe.project.id),
                "project_name": self.recipe.project.name,
                "user_id": str(self.user.id),
                "user_role": "analyst",
                "needs_correction": False,
                "retry_count": 0,
                "correction_context": {},
            }

            response = graph.invoke(initial_state, config=config)

            messages = response.get("messages", [])
            result["response"] = self._extract_response_content(messages)
            result["tools_used"] = self._extract_tools_used(messages)
            result["artifacts_created"] = self._extract_artifacts_created(messages)
            result["success"] = True

        except Exception as e:
            logger.exception(
                "Error executing recipe %s: %s",
                self.recipe.name,
                str(e),
            )
            result["error"] = str(e)
            result["success"] = False

        result["completed_at"] = timezone.now().isoformat()

        self._run.step_results = [result]
        self._run.status = (
            RecipeRunStatus.COMPLETED if result["success"] else RecipeRunStatus.FAILED
        )
        self._run.completed_at = timezone.now()
        self._run.save(update_fields=["step_results", "status", "completed_at"])

        logger.info(
            "Recipe execution finished: %s (status: %s, duration: %.2fs)",
            self.recipe.name,
            self._run.status,
            self._run.duration_seconds or 0,
        )

        return self._run

    async def execute_async(self) -> RecipeRun:
        """Execute the recipe asynchronously."""
        self.validate_variables()

        self._run = await RecipeRun.objects.acreate(
            recipe=self.recipe,
            status=RecipeRunStatus.RUNNING,
            variable_values=self.variable_values,
            step_results=[],
            started_at=timezone.now(),
            run_by=self.user,
        )

        self._thread_id = f"recipe-run-{self._run.id}"

        graph = self._build_graph()
        config = {"configurable": {"thread_id": self._thread_id}}

        prompt = self.recipe.render_prompt(self.variable_values)

        logger.info("Starting async recipe execution: %s", self.recipe.name)

        step_started = timezone.now()

        result = {
            "step_order": 1,
            "prompt": prompt,
            "response": "",
            "tools_used": [],
            "artifacts_created": [],
            "success": False,
            "error": None,
            "started_at": step_started.isoformat(),
            "completed_at": None,
        }

        try:
            initial_state = {
                "messages": [HumanMessage(content=prompt)],
                "project_id": str(self.recipe.project.id),
                "project_name": self.recipe.project.name,
                "user_id": str(self.user.id),
                "user_role": "analyst",
                "needs_correction": False,
                "retry_count": 0,
                "correction_context": {},
            }

            response = await graph.ainvoke(initial_state, config=config)

            messages = response.get("messages", [])
            result["response"] = self._extract_response_content(messages)
            result["tools_used"] = self._extract_tools_used(messages)
            result["artifacts_created"] = self._extract_artifacts_created(messages)
            result["success"] = True

        except Exception as e:
            logger.exception(
                "Error executing recipe %s (async): %s",
                self.recipe.name,
                str(e),
            )
            result["error"] = str(e)
            result["success"] = False

        result["completed_at"] = timezone.now().isoformat()

        self._run.step_results = [result]
        self._run.status = (
            RecipeRunStatus.COMPLETED if result["success"] else RecipeRunStatus.FAILED
        )
        self._run.completed_at = timezone.now()
        await self._run.asave(update_fields=["step_results", "status", "completed_at"])

        return self._run


__all__ = [
    "RecipeRunner",
    "RecipeRunnerError",
    "VariableValidationError",
    "StepExecutionError",
]
