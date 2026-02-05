"""
Recipe creation tool for the Scout data agent platform.

This module provides a tool that allows the agent to save conversation patterns
as reusable recipes. The agent can extract steps from the conversation, identify
variables for parameterization, and save them as a recipe that can be re-run.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from langchain_core.tools import tool

if TYPE_CHECKING:
    from apps.projects.models import Project
    from apps.users.models import User

logger = logging.getLogger(__name__)


# Valid variable types for recipe variables
VALID_VARIABLE_TYPES = frozenset({
    "string",
    "number",
    "date",
    "boolean",
    "select",
})


def create_recipe_tool(project: "Project", user: "User | None"):
    """
    Factory function to create a recipe saving tool for a specific project.

    The returned tool allows the agent to save a series of prompts as a reusable
    recipe with variable substitution support.

    Args:
        project: The Project model instance for scoping recipes.
        user: The User model instance who triggered the conversation.
              Used to track recipe ownership.

    Returns:
        A LangChain tool function that saves recipes.

    Example:
        >>> from apps.projects.models import Project
        >>> project = Project.objects.get(slug="analytics")
        >>> recipe_tool = create_recipe_tool(project, user)
        >>> result = recipe_tool.invoke({
        ...     "name": "Monthly Sales Report",
        ...     "description": "Generate a monthly sales summary",
        ...     "variables": [...],
        ...     "steps": [...]
        ... })
    """

    @tool
    def save_as_recipe(
        name: str,
        description: str,
        variables: list[dict[str, Any]],
        steps: list[dict[str, Any]],
        is_shared: bool = False,
    ) -> dict[str, Any]:
        """
        Save a conversation workflow as a reusable recipe with variables.

        Use this tool when the user wants to save their current analysis workflow
        as a template that can be re-run with different parameters. Extract the
        key steps from the conversation and identify values that should become
        variables.

        Args:
            name: A descriptive name for the recipe (e.g., "Monthly Sales Analysis").

            description: A longer description explaining what the recipe does and
                when to use it.

            variables: List of variable definitions. Each variable is a dict with:
                - name (str, required): Variable identifier used in {{name}} placeholders
                - type (str, required): One of "string", "number", "date", "boolean", "select"
                - label (str, required): Human-readable label for the input field
                - default (any, optional): Default value for the variable
                - options (list, optional): For type="select", list of allowed values

            steps: List of step definitions in execution order. Each step is a dict with:
                - prompt_template (str, required): The prompt with {{variable}} placeholders
                - expected_tool (str, optional): Tool the agent should use (e.g., "execute_sql")
                - description (str, optional): What this step accomplishes

            is_shared: If True, all project members can view and run this recipe.
                Default is False (only the creator can see it).

        Returns:
            A dict containing:
            - recipe_id: UUID of the created recipe (as string)
            - name: The recipe name
            - status: "created" on success, "error" on failure
            - step_count: Number of steps in the recipe
            - variable_names: List of variable names defined
            - message: Success or error message

        Example:
            >>> save_as_recipe(
            ...     name="Regional Sales Summary",
            ...     description="Generates a sales summary for a specific region and time period",
            ...     variables=[
            ...         {
            ...             "name": "region",
            ...             "type": "select",
            ...             "label": "Sales Region",
            ...             "options": ["North", "South", "East", "West"]
            ...         },
            ...         {
            ...             "name": "start_date",
            ...             "type": "date",
            ...             "label": "Start Date",
            ...             "default": "2024-01-01"
            ...         },
            ...         {
            ...             "name": "end_date",
            ...             "type": "date",
            ...             "label": "End Date"
            ...         },
            ...         {
            ...             "name": "limit",
            ...             "type": "number",
            ...             "label": "Top N Results",
            ...             "default": 10
            ...         }
            ...     ],
            ...     steps=[
            ...         {
            ...             "prompt_template": "Show me total sales for the {{region}} region between {{start_date}} and {{end_date}}",
            ...             "expected_tool": "execute_sql",
            ...             "description": "Query total sales for the region"
            ...         },
            ...         {
            ...             "prompt_template": "Now show me the top {{limit}} products by revenue in {{region}} for that period",
            ...             "expected_tool": "execute_sql",
            ...             "description": "Get top products by revenue"
            ...         },
            ...         {
            ...             "prompt_template": "Create a bar chart visualization of those top products",
            ...             "expected_tool": "create_artifact",
            ...             "description": "Visualize the results"
            ...         }
            ...     ],
            ...     is_shared=True
            ... )
            {
                "recipe_id": "123e4567-e89b-12d3-a456-426614174000",
                "name": "Regional Sales Summary",
                "status": "created",
                "step_count": 3,
                "variable_names": ["region", "start_date", "end_date", "limit"],
                "message": "Recipe 'Regional Sales Summary' created successfully with 3 steps."
            }
        """
        # Import here to avoid circular imports
        from apps.recipes.models import Recipe, RecipeStep

        # Validate name
        if not name or not name.strip():
            return {
                "recipe_id": None,
                "name": name,
                "status": "error",
                "step_count": 0,
                "variable_names": [],
                "message": "Recipe name is required.",
            }

        # Validate we have at least one step
        if not steps:
            return {
                "recipe_id": None,
                "name": name,
                "status": "error",
                "step_count": 0,
                "variable_names": [],
                "message": "At least one step is required.",
            }

        # Validate variables structure
        validated_variables = []
        for i, var in enumerate(variables):
            if not isinstance(var, dict):
                return {
                    "recipe_id": None,
                    "name": name,
                    "status": "error",
                    "step_count": 0,
                    "variable_names": [],
                    "message": f"Variable {i+1} must be a dictionary.",
                }

            var_name = var.get("name")
            var_type = var.get("type")
            var_label = var.get("label")

            if not var_name:
                return {
                    "recipe_id": None,
                    "name": name,
                    "status": "error",
                    "step_count": 0,
                    "variable_names": [],
                    "message": f"Variable {i+1} is missing 'name' field.",
                }

            if not var_type:
                return {
                    "recipe_id": None,
                    "name": name,
                    "status": "error",
                    "step_count": 0,
                    "variable_names": [],
                    "message": f"Variable '{var_name}' is missing 'type' field.",
                }

            if var_type not in VALID_VARIABLE_TYPES:
                return {
                    "recipe_id": None,
                    "name": name,
                    "status": "error",
                    "step_count": 0,
                    "variable_names": [],
                    "message": f"Variable '{var_name}' has invalid type '{var_type}'. "
                    f"Must be one of: {', '.join(sorted(VALID_VARIABLE_TYPES))}",
                }

            if not var_label:
                return {
                    "recipe_id": None,
                    "name": name,
                    "status": "error",
                    "step_count": 0,
                    "variable_names": [],
                    "message": f"Variable '{var_name}' is missing 'label' field.",
                }

            # Validate select type has options
            if var_type == "select" and not var.get("options"):
                return {
                    "recipe_id": None,
                    "name": name,
                    "status": "error",
                    "step_count": 0,
                    "variable_names": [],
                    "message": f"Variable '{var_name}' of type 'select' requires 'options' list.",
                }

            # Build validated variable
            validated_var = {
                "name": var_name,
                "type": var_type,
                "label": var_label,
            }
            if "default" in var:
                validated_var["default"] = var["default"]
            if var_type == "select":
                validated_var["options"] = var["options"]

            validated_variables.append(validated_var)

        # Validate steps structure
        validated_steps = []
        variable_names = [v["name"] for v in validated_variables]

        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                return {
                    "recipe_id": None,
                    "name": name,
                    "status": "error",
                    "step_count": 0,
                    "variable_names": [],
                    "message": f"Step {i+1} must be a dictionary.",
                }

            prompt_template = step.get("prompt_template")
            if not prompt_template or not prompt_template.strip():
                return {
                    "recipe_id": None,
                    "name": name,
                    "status": "error",
                    "step_count": 0,
                    "variable_names": [],
                    "message": f"Step {i+1} is missing 'prompt_template' field.",
                }

            # Check that referenced variables are defined
            referenced_vars = re.findall(r"\{\{(\w+)\}\}", prompt_template)
            undefined_vars = set(referenced_vars) - set(variable_names)
            if undefined_vars:
                return {
                    "recipe_id": None,
                    "name": name,
                    "status": "error",
                    "step_count": 0,
                    "variable_names": [],
                    "message": f"Step {i+1} references undefined variables: {', '.join(undefined_vars)}. "
                    f"Please define them in the variables list.",
                }

            validated_step = {
                "order": i + 1,
                "prompt_template": prompt_template.strip(),
                "expected_tool": step.get("expected_tool", "").strip(),
                "description": step.get("description", "").strip(),
            }
            validated_steps.append(validated_step)

        # Create the recipe
        try:
            recipe = Recipe.objects.create(
                project=project,
                name=name.strip(),
                description=description.strip() if description else "",
                variables=validated_variables,
                is_shared=is_shared,
                created_by=user,
            )

            # Create the steps
            for step_data in validated_steps:
                RecipeStep.objects.create(
                    recipe=recipe,
                    order=step_data["order"],
                    prompt_template=step_data["prompt_template"],
                    expected_tool=step_data["expected_tool"],
                    description=step_data["description"],
                )

            logger.info(
                "Created recipe %s for project %s with %d steps",
                recipe.id,
                project.slug,
                len(validated_steps),
            )

            return {
                "recipe_id": str(recipe.id),
                "name": recipe.name,
                "status": "created",
                "step_count": len(validated_steps),
                "variable_names": variable_names,
                "message": f"Recipe '{name}' created successfully with {len(validated_steps)} steps.",
            }

        except Exception as e:
            logger.exception(
                "Failed to create recipe for project %s: %s",
                project.slug,
                str(e),
            )
            return {
                "recipe_id": None,
                "name": name,
                "status": "error",
                "step_count": 0,
                "variable_names": [],
                "message": f"Failed to create recipe: {str(e)}",
            }

    # Set tool name explicitly
    save_as_recipe.name = "save_as_recipe"

    return save_as_recipe


__all__ = [
    "create_recipe_tool",
    "VALID_VARIABLE_TYPES",
]
