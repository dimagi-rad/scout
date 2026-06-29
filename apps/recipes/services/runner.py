"""Executes a recipe: renders its prompt with variable values, runs the agent, collects results."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from django.utils import timezone
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from apps.agents.graph.base import build_agent_graph
from apps.agents.mcp_client import get_mcp_tools
from apps.recipes.models import Recipe, RecipeRun, RecipeRunStatus
from apps.workspaces.models import Workspace

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


class RecipeRunner:
    """Executes a recipe by sending its rendered prompt to the agent."""

    def __init__(
        self,
        recipe: Recipe,
        variable_values: dict[str, Any],
        user: User,
        *,
        run: RecipeRun,
        graph: CompiledStateGraph | None = None,
        job_id: int | None = None,
    ) -> None:
        self.recipe = recipe
        self.variable_values = variable_values.copy()
        self.user = user
        self._provided_graph = graph
        self._graph: CompiledStateGraph | None = None
        # The caller (recipe_run_view -> run_recipe task) creates the RecipeRun
        # so the endpoint can return its id immediately (202) and the worker
        # updates that same row in place. The runner always operates on it.
        self._run: RecipeRun = run
        # The enclosing Procrastinate job id, forwarded to the headless
        # materialization tool for MaterializationRun traceability.
        self._job_id: int | None = job_id
        self._thread_id: str = ""

    @staticmethod
    def validate_and_default(recipe: Recipe, variable_values: dict[str, Any]) -> dict[str, Any]:
        """Validate ``variable_values`` against ``recipe`` and return a copy with
        recipe defaults applied. Raises ``VariableValidationError`` on missing
        required variables. The run endpoint uses this to validate (and return a
        400) before creating the RecipeRun, without constructing a runner.
        """
        errors = recipe.validate_variable_values(variable_values)
        if errors:
            logger.warning(
                "Variable validation failed for recipe %s: %s",
                recipe.name,
                errors,
            )
            raise VariableValidationError(errors)

        values = dict(variable_values)
        for var_def in recipe.variables:
            var_name = var_def.get("name")
            if var_name and var_name not in values and "default" in var_def:
                values[var_name] = var_def["default"]
        return values

    def validate_variables(self) -> None:
        """Validate this run's variable values, applying defaults in place."""
        self.variable_values = self.validate_and_default(self.recipe, self.variable_values)

    async def _build_graph(self) -> CompiledStateGraph:
        """Build or return the agent graph for execution (async-first)."""
        if self._provided_graph is not None:
            return self._provided_graph

        if self._graph is None:
            # Load the Workspace by FK id rather than traversing
            # ``self.recipe.workspace`` lazily, which would raise
            # SynchronousOnlyOperation under async (root of Sentry #276).
            workspace = await Workspace.objects.aget(id=self.recipe.workspace_id)
            mcp_tools = await get_mcp_tools()
            self._graph = await build_agent_graph(
                workspace=workspace,
                user=self.user,
                checkpointer=None,
                mcp_tools=mcp_tools,
                conversation_id=self._thread_id or None,
                # A recipe is a HEADLESS run: no chat Thread, no checkpointer, no
                # async-resume path. interactive=False gives the agent the
                # blocking materialize tool (not the fire-and-ack MCP one, which
                # would crash on the synthetic thread_id) and blocking prompt
                # guidance. job_id is forwarded for MaterializationRun traceability.
                interactive=False,
                job_id=self._job_id,
            )

        return self._graph

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
            if isinstance(msg, ToolMessage) and msg.name in ("create_artifact", "update_artifact"):
                try:
                    content = msg.content
                    if isinstance(content, str):
                        result = json.loads(content)
                        if isinstance(result, dict) and "artifact_id" in result:
                            artifact_ids.append(result["artifact_id"])
                except (json.JSONDecodeError, TypeError):
                    pass

        return artifact_ids

    async def execute_async(self) -> RecipeRun:
        """Execute the recipe asynchronously."""
        self.validate_variables()

        # The run is always created by the caller (the run_recipe task).
        self._run.status = RecipeRunStatus.RUNNING
        self._run.started_at = timezone.now()
        await self._run.asave(update_fields=["status", "started_at"])

        self._thread_id = f"recipe-run-{self._run.id}"

        graph = await self._build_graph()
        config = {
            "configurable": {"thread_id": self._thread_id},
            "recursion_limit": 50,
        }

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
                "workspace_id": str(self.recipe.workspace_id),
                "user_id": str(self.user.id),
                "user_role": "analyst",
                "thread_id": self._thread_id,
            }

            response = await graph.ainvoke(initial_state, config=config)

            messages = response.get("messages", [])
            result["response"] = self._extract_response_content(messages)
            result["tools_used"] = self._extract_tools_used(messages)
            result["artifacts_created"] = self._extract_artifacts_created(messages)
            result["success"] = True

        except Exception as e:
            logger.exception("Error executing recipe %s (async)", self.recipe.name)
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
]
