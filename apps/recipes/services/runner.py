"""
Recipe Runner service for the Scout data agent platform.

This module provides the RecipeRunner class which executes recipe workflows
by iterating through steps, rendering prompt templates, sending prompts to
the agent, and collecting results.

The runner maintains conversation context across steps by using the same
thread ID, allowing the agent to reference previous results and maintain
a coherent understanding of the analysis workflow.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from django.utils import timezone
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from apps.agents.graph.base import build_agent_graph
from apps.recipes.models import Recipe, RecipeRun, RecipeRunStatus, RecipeStep

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

    from apps.projects.models import Project
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
    """Raised when a step fails to execute."""

    def __init__(self, step_order: int, message: str) -> None:
        self.step_order = step_order
        super().__init__(f"Step {step_order} failed: {message}")


class RecipeRunner:
    """
    Executes a recipe workflow step by step through the agent.

    The runner:
    1. Validates that all required variables are provided
    2. Creates a RecipeRun record to track execution
    3. Iterates through recipe steps in order
    4. For each step: renders the prompt, sends it to the agent, captures results
    5. Updates the RecipeRun with results and final status

    Context is maintained across steps by using the same thread ID, allowing
    the agent to reference results from previous steps.

    Attributes:
        recipe: The recipe to execute.
        variable_values: Dictionary of variable values to substitute.
        user: The user executing the recipe.
        graph: Optional pre-built agent graph. If not provided, one will be built.

    Example:
        >>> runner = RecipeRunner(
        ...     recipe=recipe,
        ...     variable_values={"start_date": "2024-01-01", "region": "North"},
        ...     user=user,
        ... )
        >>> run = runner.execute()
        >>> print(run.status)
        'completed'
    """

    def __init__(
        self,
        recipe: Recipe,
        variable_values: dict[str, Any],
        user: "User",
        graph: "CompiledStateGraph | None" = None,
    ) -> None:
        """
        Initialize the recipe runner.

        Args:
            recipe: The recipe to execute.
            variable_values: Dictionary mapping variable names to their values.
            user: The user executing the recipe.
            graph: Optional pre-built agent graph. If not provided, one will be
                   built using the recipe's project configuration.
        """
        self.recipe = recipe
        self.variable_values = variable_values.copy()
        self.user = user
        self._provided_graph = graph
        self._graph: CompiledStateGraph | None = None
        self._run: RecipeRun | None = None
        self._thread_id: str = ""

    def validate_variables(self) -> None:
        """
        Validate that all required variables are provided.

        Checks that:
        - All required variables (those without defaults) have values
        - No unknown variables are provided
        - Select-type variables have valid option values

        Also applies default values for optional variables not provided.

        Raises:
            VariableValidationError: If validation fails.
        """
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
                    logger.debug(
                        "Applied default value for variable %s: %s",
                        var_name,
                        var_def["default"],
                    )

    def _build_graph(self) -> "CompiledStateGraph":
        """
        Build or return the agent graph for execution.

        If a graph was provided at initialization, returns that graph.
        Otherwise, builds a new graph using the recipe's project configuration.

        Returns:
            Compiled LangGraph agent graph.
        """
        if self._provided_graph is not None:
            return self._provided_graph

        if self._graph is None:
            logger.info(
                "Building agent graph for recipe %s (project: %s)",
                self.recipe.name,
                self.recipe.project.slug,
            )
            self._graph = build_agent_graph(
                project=self.recipe.project,
                user=self.user,
                checkpointer=None,  # Memory within single run, no persistence needed
            )

        return self._graph

    def _create_run_record(self) -> RecipeRun:
        """
        Create a RecipeRun record to track execution.

        Returns:
            The created RecipeRun instance with status='running'.
        """
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
        """
        Extract tool names from agent response messages.

        Looks for AIMessages with tool_calls and extracts the tool names.

        Args:
            messages: List of LangChain messages from the agent response.

        Returns:
            List of tool names that were called during execution.
        """
        tools_used = []

        for msg in messages:
            if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls"):
                for tool_call in msg.tool_calls or []:
                    tool_name = tool_call.get("name", "")
                    if tool_name and tool_name not in tools_used:
                        tools_used.append(tool_name)

        return tools_used

    def _extract_response_content(self, messages: list) -> str:
        """
        Extract the final response content from agent messages.

        Looks for the last AIMessage that contains text content (not just tool calls).

        Args:
            messages: List of LangChain messages from the agent response.

        Returns:
            The text content of the agent's response.
        """
        # Look for the last AI message with content
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content:
                # Skip messages that only have tool calls
                if hasattr(msg, "tool_calls") and msg.tool_calls and not msg.content.strip():
                    continue
                return str(msg.content)

        return ""

    def _extract_artifacts_created(self, messages: list) -> list[str]:
        """
        Extract artifact IDs from tool results in the response.

        Looks for ToolMessage responses from artifact tools and extracts
        the artifact_id from the response.

        Args:
            messages: List of LangChain messages from the agent response.

        Returns:
            List of artifact IDs created during execution.
        """
        artifact_ids = []

        for msg in messages:
            if isinstance(msg, ToolMessage):
                # Check if this is an artifact tool response
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

    def _execute_step(
        self,
        step: RecipeStep,
        graph: "CompiledStateGraph",
        config: dict,
    ) -> dict[str, Any]:
        """
        Execute a single recipe step.

        Args:
            step: The RecipeStep to execute.
            graph: The compiled agent graph.
            config: LangGraph config with thread_id for context continuity.

        Returns:
            Dictionary containing step execution results:
            - step_order: The step's order number
            - prompt: The rendered prompt that was sent
            - response: The agent's text response
            - tools_used: List of tools called during execution
            - artifacts_created: List of artifact IDs created
            - success: Whether the step completed successfully
            - error: Error message if the step failed
            - started_at: ISO timestamp when step started
            - completed_at: ISO timestamp when step completed
        """
        step_started = timezone.now()

        # Render the prompt with variable substitution
        prompt = step.render_prompt(self.variable_values)

        logger.debug(
            "Executing step %d for recipe %s: %s",
            step.order,
            self.recipe.name,
            prompt[:100] + "..." if len(prompt) > 100 else prompt,
        )

        result = {
            "step_order": step.order,
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
            # Build initial state for the agent
            initial_state = {
                "messages": [HumanMessage(content=prompt)],
                "project_id": str(self.recipe.project.id),
                "project_name": self.recipe.project.name,
                "user_id": str(self.user.id),
                "user_role": "analyst",  # Recipe execution has analyst-level access
                "needs_correction": False,
                "retry_count": 0,
                "correction_context": {},
            }

            # Invoke the agent
            response = graph.invoke(initial_state, config=config)

            # Extract results from the response
            messages = response.get("messages", [])
            result["response"] = self._extract_response_content(messages)
            result["tools_used"] = self._extract_tools_used(messages)
            result["artifacts_created"] = self._extract_artifacts_created(messages)
            result["success"] = True

            # Verify expected tool was used if specified
            if step.expected_tool:
                if step.expected_tool not in result["tools_used"]:
                    logger.warning(
                        "Step %d expected tool '%s' but got: %s",
                        step.order,
                        step.expected_tool,
                        result["tools_used"],
                    )
                    # Note: We don't fail the step, just log the warning
                    # The expected_tool is for validation/tracking, not enforcement

        except Exception as e:
            logger.exception(
                "Error executing step %d for recipe %s: %s",
                step.order,
                self.recipe.name,
                str(e),
            )
            result["error"] = str(e)
            result["success"] = False

        result["completed_at"] = timezone.now().isoformat()

        return result

    def execute(self) -> RecipeRun:
        """
        Execute the recipe and return the RecipeRun record.

        This is the main entry point for running a recipe. It:
        1. Validates variable values
        2. Creates a RecipeRun record
        3. Builds or retrieves the agent graph
        4. Executes each step in order
        5. Updates the RecipeRun with results

        The recipe run uses a single thread ID for all steps, maintaining
        conversation context so the agent can reference previous results.

        Returns:
            The RecipeRun record with execution results.

        Raises:
            VariableValidationError: If required variables are missing or invalid.
        """
        # Validate variables first
        self.validate_variables()

        # Create the run record
        self._run = self._create_run_record()

        # Build the agent graph
        graph = self._build_graph()

        # Config for maintaining conversation context across steps
        config = {"configurable": {"thread_id": self._thread_id}}

        # Execute each step in order
        steps = list(self.recipe.steps.order_by("order"))
        all_successful = True

        logger.info(
            "Starting recipe execution: %s (%d steps)",
            self.recipe.name,
            len(steps),
        )

        for step in steps:
            step_result = self._execute_step(step, graph, config)

            # Add result to the run
            self._run.step_results.append(step_result)
            self._run.save(update_fields=["step_results"])

            if not step_result["success"]:
                all_successful = False
                logger.error(
                    "Recipe %s failed at step %d: %s",
                    self.recipe.name,
                    step.order,
                    step_result.get("error", "Unknown error"),
                )
                # Stop execution on failure
                break

            logger.info(
                "Completed step %d/%d for recipe %s",
                step.order,
                len(steps),
                self.recipe.name,
            )

        # Update final status
        self._run.status = (
            RecipeRunStatus.COMPLETED if all_successful else RecipeRunStatus.FAILED
        )
        self._run.completed_at = timezone.now()
        self._run.save(update_fields=["status", "completed_at"])

        logger.info(
            "Recipe execution finished: %s (status: %s, duration: %.2fs)",
            self.recipe.name,
            self._run.status,
            self._run.duration_seconds or 0,
        )

        return self._run

    async def execute_async(self) -> RecipeRun:
        """
        Execute the recipe asynchronously and return the RecipeRun record.

        This is the async version of execute() for use in async contexts.
        It provides the same functionality but uses async graph invocation.

        Returns:
            The RecipeRun record with execution results.

        Raises:
            VariableValidationError: If required variables are missing or invalid.
        """
        # Validate variables first
        self.validate_variables()

        # Create the run record
        self._run = await RecipeRun.objects.acreate(
            recipe=self.recipe,
            status=RecipeRunStatus.RUNNING,
            variable_values=self.variable_values,
            step_results=[],
            started_at=timezone.now(),
            run_by=self.user,
        )

        self._thread_id = f"recipe-run-{self._run.id}"

        logger.info(
            "Created recipe run %s for recipe %s (thread_id: %s)",
            self._run.id,
            self.recipe.name,
            self._thread_id,
        )

        # Build the agent graph
        graph = self._build_graph()

        # Config for maintaining conversation context across steps
        config = {"configurable": {"thread_id": self._thread_id}}

        # Execute each step in order
        steps = [step async for step in self.recipe.steps.order_by("order")]
        all_successful = True

        logger.info(
            "Starting async recipe execution: %s (%d steps)",
            self.recipe.name,
            len(steps),
        )

        for step in steps:
            step_result = await self._execute_step_async(step, graph, config)

            # Add result to the run
            self._run.step_results.append(step_result)
            await self._run.asave(update_fields=["step_results"])

            if not step_result["success"]:
                all_successful = False
                logger.error(
                    "Recipe %s failed at step %d: %s",
                    self.recipe.name,
                    step.order,
                    step_result.get("error", "Unknown error"),
                )
                # Stop execution on failure
                break

            logger.info(
                "Completed step %d/%d for recipe %s",
                step.order,
                len(steps),
                self.recipe.name,
            )

        # Update final status
        self._run.status = (
            RecipeRunStatus.COMPLETED if all_successful else RecipeRunStatus.FAILED
        )
        self._run.completed_at = timezone.now()
        await self._run.asave(update_fields=["status", "completed_at"])

        logger.info(
            "Async recipe execution finished: %s (status: %s, duration: %.2fs)",
            self.recipe.name,
            self._run.status,
            self._run.duration_seconds or 0,
        )

        return self._run

    async def _execute_step_async(
        self,
        step: RecipeStep,
        graph: "CompiledStateGraph",
        config: dict,
    ) -> dict[str, Any]:
        """
        Execute a single recipe step asynchronously.

        This is the async version of _execute_step().

        Args:
            step: The RecipeStep to execute.
            graph: The compiled agent graph.
            config: LangGraph config with thread_id for context continuity.

        Returns:
            Dictionary containing step execution results.
        """
        step_started = timezone.now()

        # Render the prompt with variable substitution
        prompt = step.render_prompt(self.variable_values)

        logger.debug(
            "Executing step %d for recipe %s (async): %s",
            step.order,
            self.recipe.name,
            prompt[:100] + "..." if len(prompt) > 100 else prompt,
        )

        result = {
            "step_order": step.order,
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
            # Build initial state for the agent
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

            # Invoke the agent asynchronously
            response = await graph.ainvoke(initial_state, config=config)

            # Extract results from the response
            messages = response.get("messages", [])
            result["response"] = self._extract_response_content(messages)
            result["tools_used"] = self._extract_tools_used(messages)
            result["artifacts_created"] = self._extract_artifacts_created(messages)
            result["success"] = True

            # Verify expected tool was used if specified
            if step.expected_tool:
                if step.expected_tool not in result["tools_used"]:
                    logger.warning(
                        "Step %d expected tool '%s' but got: %s",
                        step.order,
                        step.expected_tool,
                        result["tools_used"],
                    )

        except Exception as e:
            logger.exception(
                "Error executing step %d for recipe %s (async): %s",
                step.order,
                self.recipe.name,
                str(e),
            )
            result["error"] = str(e)
            result["success"] = False

        result["completed_at"] = timezone.now().isoformat()

        return result


__all__ = [
    "RecipeRunner",
    "RecipeRunnerError",
    "VariableValidationError",
    "StepExecutionError",
]
