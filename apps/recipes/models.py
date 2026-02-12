"""
Recipe models for the Scout data agent platform.

Defines Recipe, RecipeStep, and RecipeRun models for creating and executing
reusable conversation workflows with variable substitution.
"""
import secrets
import uuid

from django.conf import settings
from django.db import models


class Recipe(models.Model):
    """
    A reusable workflow template that defines a sequence of prompts with variables.

    Recipes allow users to save common analysis patterns that can be re-run with
    different variable values. Each recipe belongs to a project and can optionally
    be shared with all project members.

    Variables are defined as a list of dictionaries with the following structure:
    [
        {
            "name": "variable_name",
            "type": "string|number|date|boolean|select",
            "label": "Human-readable label",
            "default": "optional default value",
            "options": ["opt1", "opt2"]  # Only for type="select"
        },
        ...
    ]
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="recipes",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    # Variable definitions - list of variable specs
    variables = models.JSONField(
        default=list,
        blank=True,
        help_text="List of variable definitions for the recipe.",
    )

    # Sharing settings
    is_shared = models.BooleanField(
        default=False,
        help_text="If true, all project members can view and run this recipe.",
    )
    is_public = models.BooleanField(
        default=False,
        help_text="If true, accessible via public share link without authentication.",
    )
    share_token = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="Token for public share URL. Auto-generated when is_public is set.",
    )

    # Metadata
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_recipes",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["project", "is_shared"]),
            models.Index(fields=["project", "created_by"]),
        ]

    def save(self, *args, **kwargs):
        if self.is_public and not self.share_token:
            self.share_token = secrets.token_urlsafe(32)
        elif not self.is_public:
            self.share_token = None
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.project.name})"

    def get_variable_names(self) -> list[str]:
        """Return a list of variable names defined in this recipe."""
        return [var.get("name") for var in self.variables if var.get("name")]

    def validate_variable_values(self, values: dict) -> list[str]:
        """
        Validate that provided variable values match the recipe's variable definitions.

        Args:
            values: Dictionary mapping variable names to their values.

        Returns:
            List of validation error messages (empty if valid).
        """
        from datetime import datetime

        errors = []
        required_vars = set(self.get_variable_names())
        provided_vars = set(values.keys())

        # Check for missing required variables (those without defaults)
        for var_def in self.variables:
            var_name = var_def.get("name")
            if var_name and var_name not in provided_vars:
                if "default" not in var_def:
                    errors.append(f"Missing required variable: {var_name}")

        # Check for unknown variables
        unknown = provided_vars - required_vars
        if unknown:
            errors.append(f"Unknown variables: {', '.join(unknown)}")

        # Type validation for all variable types
        for var_def in self.variables:
            var_name = var_def.get("name")
            var_type = var_def.get("type", "string")

            if var_name not in values:
                continue

            value = values[var_name]

            # Skip validation for empty/None values if variable has a default
            if value is None or value == "":
                continue

            if var_type == "select":
                options = var_def.get("options", [])
                if options and value not in options:
                    errors.append(
                        f"Invalid value for {var_name}: must be one of {options}"
                    )
            elif var_type == "number":
                try:
                    float(value)
                except (ValueError, TypeError):
                    errors.append(f"Invalid number for {var_name}: {value}")
            elif var_type == "boolean":
                if isinstance(value, str):
                    if value.lower() not in ("true", "false", "1", "0", "yes", "no"):
                        errors.append(f"Invalid boolean for {var_name}: {value}")
                elif not isinstance(value, bool):
                    errors.append(f"Invalid boolean for {var_name}: {value}")
            elif var_type == "date":
                if isinstance(value, str):
                    # Try ISO format (YYYY-MM-DD)
                    try:
                        datetime.strptime(value, "%Y-%m-%d")
                    except ValueError:
                        errors.append(
                            f"Invalid date for {var_name}: {value} (expected YYYY-MM-DD format)"
                        )

        return errors


class RecipeStep(models.Model):
    """
    A single step in a recipe workflow.

    Each step contains a prompt template that can include {{variable}} placeholders.
    Steps are executed in order and can optionally specify an expected tool that
    should be used by the agent.

    The prompt_template supports variable substitution using double curly braces:
    "Show me the top {{limit}} customers from {{region}} for {{date_range}}"
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.CASCADE,
        related_name="steps",
    )
    order = models.PositiveIntegerField(
        help_text="Execution order of this step (starting from 1).",
    )
    prompt_template = models.TextField(
        help_text="Prompt template with {{variable}} placeholders.",
    )
    expected_tool = models.CharField(
        max_length=100,
        blank=True,
        help_text="Optional: expected tool the agent should use (e.g., 'execute_sql').",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional description of what this step accomplishes.",
    )

    class Meta:
        ordering = ["recipe", "order"]
        unique_together = ["recipe", "order"]

    def __str__(self):
        return f"Step {self.order}: {self.recipe.name}"

    def render_prompt(self, variable_values: dict) -> str:
        """
        Render the prompt template by substituting variable values.

        Args:
            variable_values: Dictionary mapping variable names to their values.

        Returns:
            The rendered prompt with all variables substituted.

        Example:
            >>> step.prompt_template = "Show top {{limit}} from {{region}}"
            >>> step.render_prompt({"limit": 10, "region": "North"})
            "Show top 10 from North"
        """
        prompt = self.prompt_template
        for name, value in variable_values.items():
            placeholder = "{{" + name + "}}"
            prompt = prompt.replace(placeholder, str(value))
        return prompt


class RecipeRunStatus(models.TextChoices):
    """Status choices for recipe run execution."""

    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class RecipeRun(models.Model):
    """
    Tracks the execution of a recipe with specific variable values.

    Each run records the variable values used, the results from each step,
    and timing information for the entire execution.

    Step results are stored as a list of dictionaries:
    [
        {
            "step_order": 1,
            "prompt": "rendered prompt",
            "response": "agent response",
            "tool_used": "execute_sql",
            "started_at": "2024-01-15T10:30:00Z",
            "completed_at": "2024-01-15T10:30:05Z",
            "error": null
        },
        ...
    ]
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.CASCADE,
        related_name="runs",
    )
    status = models.CharField(
        max_length=20,
        choices=RecipeRunStatus.choices,
        default=RecipeRunStatus.PENDING,
    )

    # Variable values used for this run
    variable_values = models.JSONField(
        default=dict,
        help_text="Actual variable values used for this run.",
    )

    # Results from each step
    step_results = models.JSONField(
        default=list,
        blank=True,
        help_text="Results from each step execution.",
    )

    # Timing
    started_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    # Sharing settings
    is_shared = models.BooleanField(
        default=False,
        help_text="Visible to all project members.",
    )
    is_public = models.BooleanField(
        default=False,
        help_text="If true, accessible via public share link without authentication.",
    )
    share_token = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="Token for public share URL. Auto-generated when is_public is set.",
    )

    # Who ran this
    run_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="recipe_runs",
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipe", "-created_at"]),
            models.Index(fields=["run_by", "-created_at"]),
            models.Index(fields=["status"]),
        ]

    def save(self, *args, **kwargs):
        if self.is_public and not self.share_token:
            self.share_token = secrets.token_urlsafe(32)
        elif not self.is_public:
            self.share_token = None
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Run of {self.recipe.name} ({self.status})"

    @property
    def duration_seconds(self) -> float | None:
        """Calculate the duration of the run in seconds."""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    @property
    def current_step(self) -> int:
        """Return the current step number (1-indexed) based on step_results."""
        return len(self.step_results) + 1 if self.status == RecipeRunStatus.RUNNING else 0

    def add_step_result(
        self,
        step_order: int,
        prompt: str,
        response: str,
        tool_used: str | None = None,
        error: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        """
        Add a step result to the run.

        Args:
            step_order: The order of the step that was executed.
            prompt: The rendered prompt that was sent.
            response: The agent's response.
            tool_used: Optional name of the tool used by the agent.
            error: Optional error message if the step failed.
            started_at: ISO format timestamp when step started.
            completed_at: ISO format timestamp when step completed.
        """
        result = {
            "step_order": step_order,
            "prompt": prompt,
            "response": response,
            "tool_used": tool_used,
            "started_at": started_at,
            "completed_at": completed_at,
            "error": error,
        }
        self.step_results.append(result)
        self.save(update_fields=["step_results"])
