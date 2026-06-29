"""Recipe, RecipeStep, and RecipeRun models for reusable conversation workflows."""

import secrets
import uuid

from django.conf import settings
from django.db import models


class RecipeSoftDeleteManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class Recipe(models.Model):
    """
    A reusable workflow template: a prompt with variables, re-run with different values.

    Variables are a list of dicts:
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
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="recipes",
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    prompt = models.TextField(
        blank=True,
        default="",
        help_text="Prompt template with {{variable}} placeholders. Supports markdown.",
    )

    variables = models.JSONField(
        default=list,
        blank=True,
        help_text="List of variable definitions for the recipe.",
    )

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

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_recipes",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    objects = RecipeSoftDeleteManager()
    all_objects = models.Manager()  # noqa: DJ012 — ruff misclassifies Manager() as a field

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["workspace", "is_shared"]),
            models.Index(fields=["workspace", "created_by"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.workspace})"

    def save(self, *args, **kwargs):
        if self.is_public and not self.share_token:
            self.share_token = secrets.token_urlsafe(32)
        elif not self.is_public:
            self.share_token = None
        super().save(*args, **kwargs)

    def soft_delete(self, deleted_by) -> None:
        from django.utils import timezone

        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.deleted_by = deleted_by
        self.save(update_fields=["is_deleted", "deleted_at", "deleted_by"])

    def undelete(self) -> None:
        self.is_deleted = False
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=["is_deleted", "deleted_at", "deleted_by"])

    def get_variable_names(self) -> list[str]:
        """Return a list of variable names defined in this recipe."""
        return [var.get("name") for var in self.variables if var.get("name")]

    def render_prompt(self, variable_values: dict) -> str:
        """Render the prompt template by substituting variable values."""
        rendered = self.prompt
        for name, value in variable_values.items():
            placeholder = "{{" + name + "}}"
            rendered = rendered.replace(placeholder, str(value))
        return rendered

    def validate_variable_values(self, values: dict) -> list[str]:
        """Validate ``values`` against variable definitions; return error messages (empty if valid)."""
        from datetime import datetime

        errors = []
        required_vars = set(self.get_variable_names())
        provided_vars = set(values.keys())

        # Missing required variables are those without defaults.
        for var_def in self.variables:
            var_name = var_def.get("name")
            if var_name and var_name not in provided_vars and "default" not in var_def:
                errors.append(f"Missing required variable: {var_name}")

        unknown = provided_vars - required_vars
        if unknown:
            errors.append(f"Unknown variables: {', '.join(unknown)}")

        for var_def in self.variables:
            var_name = var_def.get("name")
            var_type = var_def.get("type", "string")

            if var_name not in values:
                continue

            value = values[var_name]

            if value is None or value == "":
                continue

            if var_type == "select":
                options = var_def.get("options", [])
                if options and value not in options:
                    errors.append(f"Invalid value for {var_name}: must be one of {options}")
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
                    try:
                        datetime.strptime(value, "%Y-%m-%d")
                    except ValueError:
                        errors.append(
                            f"Invalid date for {var_name}: {value} (expected YYYY-MM-DD format)"
                        )

        return errors


class RecipeStep(models.Model):
    """A single ordered step in a recipe workflow with a {{variable}} prompt template."""

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
        """Render the prompt template by substituting variable values."""
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

    Step results are stored as a list of dicts:
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

    variable_values = models.JSONField(
        default=dict,
        help_text="Actual variable values used for this run.",
    )

    step_results = models.JSONField(
        default=list,
        blank=True,
        help_text="Results from each step execution.",
    )

    started_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

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

    run_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="recipe_runs",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipe", "-created_at"]),
            models.Index(fields=["run_by", "-created_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"Run of {self.recipe.name} ({self.status})"

    def save(self, *args, **kwargs):
        if self.is_public and not self.share_token:
            self.share_token = secrets.token_urlsafe(32)
        elif not self.is_public:
            self.share_token = None
        super().save(*args, **kwargs)

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
        """Append a step result to the run and persist it."""
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
