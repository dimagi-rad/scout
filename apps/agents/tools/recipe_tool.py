"""
Recipe creation tool for the Scout data agent platform.

This module provides a tool that allows the agent to save conversation patterns
as reusable recipes. The agent can extract the workflow as a prompt template,
identify variables for parameterization, and save them as a recipe that can be
re-run.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from langchain_core.tools import tool

if TYPE_CHECKING:
    from apps.projects.models import TenantWorkspace
    from apps.users.models import User

logger = logging.getLogger(__name__)


# Valid variable types for recipe variables
VALID_VARIABLE_TYPES = frozenset(
    {
        "string",
        "number",
        "date",
        "boolean",
        "select",
    }
)


def create_recipe_tool(workspace: TenantWorkspace, user: User | None):
    """
    Factory function to create a recipe saving tool for a specific workspace.

    The returned tool allows the agent to save a prompt template as a reusable
    recipe with variable substitution support.

    Args:
        workspace: The TenantWorkspace model instance for scoping recipes.
        user: The User model instance who triggered the conversation.
              Used to track recipe ownership.

    Returns:
        A LangChain tool function that saves recipes.
    """

    @tool
    def save_as_recipe(
        name: str,
        description: str,
        variables: list[dict[str, Any]],
        prompt: str,
        is_shared: bool = False,
    ) -> dict[str, Any]:
        """
        Save a conversation workflow as a reusable recipe with variables.

        Use this tool when the user wants to save their current analysis workflow
        as a template that can be re-run with different parameters. Extract the
        key instructions from the conversation and identify values that should
        become variables.

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

            prompt: The prompt template with {{variable}} placeholders. This is a
                markdown-formatted instruction that will be sent to the agent when
                the recipe is run. Use {{variable_name}} syntax for parameterized values.

            is_shared: If True, all workspace members can view and run this recipe.
                Default is False (only the creator can see it).

        Returns:
            A dict containing:
            - recipe_id: UUID of the created recipe (as string)
            - name: The recipe name
            - status: "created" on success, "error" on failure
            - variable_names: List of variable names defined
            - message: Success or error message
        """
        # Import here to avoid circular imports
        from apps.recipes.models import Recipe

        # Validate name
        if not name or not name.strip():
            return {
                "recipe_id": None,
                "name": name,
                "status": "error",
                "variable_names": [],
                "message": "Recipe name is required.",
            }

        # Validate prompt
        if not prompt or not prompt.strip():
            return {
                "recipe_id": None,
                "name": name,
                "status": "error",
                "variable_names": [],
                "message": "Prompt is required.",
            }

        # Validate variables structure
        validated_variables = []
        for i, var in enumerate(variables):
            if not isinstance(var, dict):
                return {
                    "recipe_id": None,
                    "name": name,
                    "status": "error",
                    "variable_names": [],
                    "message": f"Variable {i + 1} must be a dictionary.",
                }

            var_name = var.get("name")
            var_type = var.get("type")
            var_label = var.get("label")

            if not var_name:
                return {
                    "recipe_id": None,
                    "name": name,
                    "status": "error",
                    "variable_names": [],
                    "message": f"Variable {i + 1} is missing 'name' field.",
                }

            if not var_type:
                return {
                    "recipe_id": None,
                    "name": name,
                    "status": "error",
                    "variable_names": [],
                    "message": f"Variable '{var_name}' is missing 'type' field.",
                }

            if var_type not in VALID_VARIABLE_TYPES:
                return {
                    "recipe_id": None,
                    "name": name,
                    "status": "error",
                    "variable_names": [],
                    "message": f"Variable '{var_name}' has invalid type '{var_type}'. "
                    f"Must be one of: {', '.join(sorted(VALID_VARIABLE_TYPES))}",
                }

            if not var_label:
                return {
                    "recipe_id": None,
                    "name": name,
                    "status": "error",
                    "variable_names": [],
                    "message": f"Variable '{var_name}' is missing 'label' field.",
                }

            # Validate select type has options
            if var_type == "select" and not var.get("options"):
                return {
                    "recipe_id": None,
                    "name": name,
                    "status": "error",
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

        # Validate that referenced variables in prompt are defined
        variable_names = [v["name"] for v in validated_variables]
        referenced_vars = re.findall(r"\{\{(\w+)\}\}", prompt)
        undefined_vars = set(referenced_vars) - set(variable_names)
        if undefined_vars:
            return {
                "recipe_id": None,
                "name": name,
                "status": "error",
                "variable_names": [],
                "message": f"Prompt references undefined variables: {', '.join(undefined_vars)}. "
                f"Please define them in the variables list.",
            }

        # Create the recipe
        try:
            recipe = Recipe.objects.create(
                workspace=workspace,
                name=name.strip(),
                description=description.strip() if description else "",
                prompt=prompt.strip(),
                variables=validated_variables,
                is_shared=is_shared,
                created_by=user,
            )

            logger.info(
                "Created recipe %s for workspace %s",
                recipe.id,
                workspace.tenant_id,
            )

            return {
                "recipe_id": str(recipe.id),
                "name": recipe.name,
                "status": "created",
                "variable_names": variable_names,
                "message": f"Recipe '{name}' created successfully.",
            }

        except Exception as e:
            logger.exception(
                "Failed to create recipe for workspace %s: %s",
                workspace.tenant_id,
                str(e),
            )
            return {
                "recipe_id": None,
                "name": name,
                "status": "error",
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
