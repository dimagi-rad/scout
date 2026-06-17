"""
Comprehensive tests for Phase 4 (Recipes) of the Scout data agent platform.

Tests recipe CRUD, variable substitution, recipe runner, and save_as_recipe tool.
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import AsyncClient
from django.utils import timezone
from langchain_core.messages import AIMessage

from apps.recipes.models import Recipe, RecipeRun, RecipeRunStatus, RecipeStep
from apps.recipes.services.runner import RecipeRunner, VariableValidationError

User = get_user_model()


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def recipe(db, user, workspace):
    """Create a test recipe with variables."""
    return Recipe.objects.create(
        workspace=workspace,
        name="Sales Analysis",
        description="Analyze sales data for a specific region and time period",
        prompt="Show me the top {{limit}} customers in {{region}} region starting from {{start_date}}",
        variables=[
            {
                "name": "region",
                "type": "select",
                "label": "Region",
                "default": "North",
                "options": ["North", "South", "East", "West"],
            },
            {
                "name": "limit",
                "type": "number",
                "label": "Number of results",
                "default": 10,
            },
            {
                "name": "start_date",
                "type": "date",
                "label": "Start Date",
            },
        ],
        is_shared=False,
        created_by=user,
    )


@pytest.fixture
def recipe_step_1(db, recipe):
    """Create first step of a recipe."""
    return RecipeStep.objects.create(
        recipe=recipe,
        order=1,
        prompt_template="Show me the top {{limit}} customers in {{region}} region",
        expected_tool="execute_sql",
        description="Get top customers by region",
    )


@pytest.fixture
def recipe_step_2(db, recipe):
    """Create second step of a recipe."""
    return RecipeStep.objects.create(
        recipe=recipe,
        order=2,
        prompt_template="What were the total sales for {{region}} starting from {{start_date}}?",
        expected_tool="execute_sql",
        description="Calculate total sales for region",
    )


@pytest.fixture
def recipe_run(db, recipe, user):
    """Create a recipe run instance."""
    return RecipeRun.objects.create(
        recipe=recipe,
        status=RecipeRunStatus.PENDING,
        variable_values={
            "region": "North",
            "limit": 5,
            "start_date": "2024-01-01",
        },
        run_by=user,
    )


# ============================================================================
# 1. TestRecipeModel
# ============================================================================


@pytest.mark.django_db
class TestRecipeModel:
    """Tests for the Recipe model CRUD operations."""

    def test_create_recipe(self, user, workspace):
        """Test creating a basic recipe."""
        recipe = Recipe.objects.create(
            workspace=workspace,
            name="Customer Report",
            description="Generate customer analysis report",
            variables=[
                {
                    "name": "year",
                    "type": "number",
                    "label": "Year",
                    "default": 2024,
                }
            ],
            is_shared=True,
            created_by=user,
        )

        assert recipe.id is not None
        assert recipe.name == "Customer Report"
        assert recipe.description == "Generate customer analysis report"
        assert len(recipe.variables) == 1
        assert recipe.variables[0]["name"] == "year"
        assert recipe.is_shared is True
        assert recipe.created_by == user
        assert recipe.workspace == workspace
        assert str(recipe) == f"Customer Report ({workspace.tenant_name})"

    def test_read_recipe(self, recipe):
        """Test reading an existing recipe."""
        fetched_recipe = Recipe.objects.get(id=recipe.id)

        assert fetched_recipe.name == recipe.name
        assert fetched_recipe.description == recipe.description
        assert fetched_recipe.variables == recipe.variables
        assert fetched_recipe.workspace == recipe.workspace

    def test_update_recipe(self, recipe):
        """Test updating a recipe."""
        recipe.name = "Updated Sales Analysis"
        recipe.description = "Updated description"
        recipe.is_shared = True
        recipe.save()

        updated_recipe = Recipe.objects.get(id=recipe.id)
        assert updated_recipe.name == "Updated Sales Analysis"
        assert updated_recipe.description == "Updated description"
        assert updated_recipe.is_shared is True

    def test_delete_recipe(self, recipe):
        """Test deleting a recipe."""
        recipe_id = recipe.id
        recipe.delete()

        assert not Recipe.objects.filter(id=recipe_id).exists()

    def test_recipe_ordering(self, user, workspace):
        """Test that recipes are ordered by updated_at descending."""
        recipe1 = Recipe.objects.create(
            workspace=workspace,
            name="Recipe 1",
            created_by=user,
        )
        recipe2 = Recipe.objects.create(
            workspace=workspace,
            name="Recipe 2",
            created_by=user,
        )

        recipes = list(Recipe.objects.filter(workspace=workspace))
        # Most recently updated should be first
        assert recipes[0].id == recipe2.id
        assert recipes[1].id == recipe1.id

    def test_recipe_workspace_relationship(self, recipe, workspace):
        """Test that recipe is properly linked to workspace."""
        assert recipe.workspace == workspace
        assert recipe in workspace.recipes.all()

    def test_get_variable_names(self, recipe):
        """Test getting list of variable names from recipe."""
        variable_names = recipe.get_variable_names()

        assert len(variable_names) == 3
        assert "region" in variable_names
        assert "limit" in variable_names
        assert "start_date" in variable_names


# ============================================================================
# 2. TestRecipeVariableValidation
# ============================================================================


@pytest.mark.django_db
class TestRecipeVariableValidation:
    """Tests for recipe variable validation."""

    def test_validate_all_required_variables_provided(self, recipe):
        """Test validation passes when all required variables are provided."""
        values = {
            "region": "South",
            "limit": 20,
            "start_date": "2024-06-01",
        }

        errors = recipe.validate_variable_values(values)
        assert len(errors) == 0

    def test_validate_missing_required_variable(self, recipe):
        """Test validation fails when required variable is missing."""
        # start_date has no default, so it's required
        values = {
            "region": "North",
            "limit": 10,
            # Missing start_date
        }

        errors = recipe.validate_variable_values(values)
        assert len(errors) > 0
        assert any("start_date" in error for error in errors)

    def test_validate_optional_variable_can_be_omitted(self, recipe):
        """Test that variables with defaults can be omitted."""
        # region and limit have defaults, so they're optional
        # start_date has NO default, so it's required
        values = {
            "start_date": "2024-01-01",
            # Omitting region and limit (they have defaults)
        }

        # Region and limit have defaults, so validation should pass
        # even though they're not provided
        errors = recipe.validate_variable_values(values)

        # Should not have errors for region or limit (they have defaults)
        # The validate_variable_values method only errors on REQUIRED variables
        # Variables with defaults are optional
        region_errors = [e for e in errors if "region" in e.lower()]
        limit_errors = [e for e in errors if "limit" in e.lower()]

        # These should be empty because defaults are provided
        assert len(region_errors) == 0
        assert len(limit_errors) == 0

    def test_validate_unknown_variable(self, recipe):
        """Test validation fails when unknown variable is provided."""
        values = {
            "region": "North",
            "limit": 10,
            "start_date": "2024-01-01",
            "unknown_var": "value",  # Not in recipe definition
        }

        errors = recipe.validate_variable_values(values)
        assert len(errors) > 0
        assert any("unknown" in error.lower() for error in errors)

    def test_validate_select_field_valid_option(self, recipe):
        """Test validation passes for select field with valid option."""
        values = {
            "region": "South",  # Valid option
            "limit": 10,
            "start_date": "2024-01-01",
        }

        errors = recipe.validate_variable_values(values)
        # Should not have error about region
        region_errors = [e for e in errors if "region" in e.lower()]
        assert len(region_errors) == 0

    def test_validate_select_field_invalid_option(self, recipe):
        """Test validation fails for select field with invalid option."""
        values = {
            "region": "InvalidRegion",  # Not in options
            "limit": 10,
            "start_date": "2024-01-01",
        }

        errors = recipe.validate_variable_values(values)
        assert len(errors) > 0
        assert any("region" in error.lower() for error in errors)

    def test_validate_empty_values(self, recipe):
        """Test validation with empty values dictionary."""
        values = {}

        errors = recipe.validate_variable_values(values)
        # Should have errors for all required variables without defaults
        assert len(errors) > 0


# ============================================================================
# 3. TestRecipeStepModel
# ============================================================================


@pytest.mark.django_db
class TestRecipeStepModel:
    """Tests for the RecipeStep model."""

    def test_create_recipe_step(self, recipe):
        """Test creating a recipe step."""
        step = RecipeStep.objects.create(
            recipe=recipe,
            order=1,
            prompt_template="Show sales for {{region}}",
            expected_tool="execute_sql",
            description="Get sales data",
        )

        assert step.id is not None
        assert step.recipe == recipe
        assert step.order == 1
        assert step.prompt_template == "Show sales for {{region}}"
        assert step.expected_tool == "execute_sql"
        assert str(step) == f"Step 1: {recipe.name}"

    def test_recipe_step_ordering(self, recipe):
        """Test that recipe steps are ordered by recipe and order."""
        RecipeStep.objects.create(recipe=recipe, order=1, prompt_template="Step 1")
        RecipeStep.objects.create(recipe=recipe, order=2, prompt_template="Step 2")
        RecipeStep.objects.create(recipe=recipe, order=3, prompt_template="Step 3")

        steps = list(recipe.steps.all())
        assert len(steps) == 3
        assert steps[0].order == 1
        assert steps[1].order == 2
        assert steps[2].order == 3

    def test_recipe_step_unique_order_per_recipe(self, recipe):
        """Test that order must be unique within a recipe."""
        RecipeStep.objects.create(recipe=recipe, order=1, prompt_template="Step 1")

        # Creating another step with same order should fail
        with pytest.raises(IntegrityError):
            RecipeStep.objects.create(recipe=recipe, order=1, prompt_template="Duplicate")

    def test_recipe_cascade_delete_steps(self, recipe):
        """Test that deleting a recipe deletes its steps."""
        RecipeStep.objects.create(recipe=recipe, order=1, prompt_template="Step 1")
        RecipeStep.objects.create(recipe=recipe, order=2, prompt_template="Step 2")

        recipe_id = recipe.id
        recipe.delete()

        # Steps should be deleted
        assert not RecipeStep.objects.filter(recipe_id=recipe_id).exists()


# ============================================================================
# 4. TestRecipeStepVariableSubstitution
# ============================================================================


@pytest.mark.django_db
class TestRecipeStepVariableSubstitution:
    """Tests for variable substitution in prompt templates."""

    def test_render_prompt_single_variable(self, recipe):
        """Test rendering prompt with single variable."""
        step = RecipeStep.objects.create(
            recipe=recipe,
            order=1,
            prompt_template="Show data for {{region}}",
        )

        rendered = step.render_prompt({"region": "North"})
        assert rendered == "Show data for North"

    def test_render_prompt_multiple_variables(self, recipe):
        """Test rendering prompt with multiple variables."""
        step = RecipeStep.objects.create(
            recipe=recipe,
            order=1,
            prompt_template="Show top {{limit}} customers in {{region}}",
        )

        rendered = step.render_prompt({"region": "South", "limit": 25})
        assert rendered == "Show top 25 customers in South"

    def test_render_prompt_repeated_variable(self, recipe):
        """Test rendering prompt with same variable used multiple times."""
        step = RecipeStep.objects.create(
            recipe=recipe,
            order=1,
            prompt_template="{{region}} sales: compare {{region}} to other regions",
        )

        rendered = step.render_prompt({"region": "West"})
        assert rendered == "West sales: compare West to other regions"

    def test_render_prompt_no_variables(self, recipe):
        """Test rendering prompt without any variables."""
        step = RecipeStep.objects.create(
            recipe=recipe,
            order=1,
            prompt_template="Show all sales data",
        )

        rendered = step.render_prompt({})
        assert rendered == "Show all sales data"

    def test_render_prompt_extra_variables_ignored(self, recipe):
        """Test that extra variables in values dict are ignored."""
        step = RecipeStep.objects.create(
            recipe=recipe,
            order=1,
            prompt_template="Show {{region}} data",
        )

        rendered = step.render_prompt(
            {
                "region": "East",
                "unused_var": "value",
            }
        )
        assert rendered == "Show East data"

    def test_render_prompt_number_variable(self, recipe):
        """Test rendering with number variable."""
        step = RecipeStep.objects.create(
            recipe=recipe,
            order=1,
            prompt_template="Show top {{limit}} results",
        )

        rendered = step.render_prompt({"limit": 100})
        assert rendered == "Show top 100 results"

    def test_render_prompt_date_variable(self, recipe):
        """Test rendering with date variable."""
        step = RecipeStep.objects.create(
            recipe=recipe,
            order=1,
            prompt_template="Sales since {{start_date}}",
        )

        rendered = step.render_prompt({"start_date": "2024-01-01"})
        assert rendered == "Sales since 2024-01-01"


# ============================================================================
# 5. TestRecipeRunModel
# ============================================================================


@pytest.mark.django_db
class TestRecipeRunModel:
    """Tests for the RecipeRun model."""

    def test_create_recipe_run(self, recipe, user):
        """Test creating a recipe run."""
        run = RecipeRun.objects.create(
            recipe=recipe,
            status=RecipeRunStatus.PENDING,
            variable_values={"region": "North", "limit": 10, "start_date": "2024-01-01"},
            run_by=user,
        )

        assert run.id is not None
        assert run.recipe == recipe
        assert run.status == RecipeRunStatus.PENDING
        assert run.variable_values["region"] == "North"
        assert run.run_by == user
        assert run.started_at is None
        assert run.completed_at is None
        assert str(run) == f"Run of {recipe.name} (pending)"

    def test_recipe_run_status_transitions(self, recipe_run):
        """Test recipe run status transitions."""
        assert recipe_run.status == RecipeRunStatus.PENDING

        # Start running
        recipe_run.status = RecipeRunStatus.RUNNING
        recipe_run.started_at = timezone.now()
        recipe_run.save()
        assert recipe_run.status == RecipeRunStatus.RUNNING

        # Complete
        recipe_run.status = RecipeRunStatus.COMPLETED
        recipe_run.completed_at = timezone.now()
        recipe_run.save()
        assert recipe_run.status == RecipeRunStatus.COMPLETED

    def test_recipe_run_failed_status(self, recipe_run):
        """Test recipe run can be marked as failed."""
        recipe_run.status = RecipeRunStatus.FAILED
        recipe_run.started_at = timezone.now()
        recipe_run.save()

        assert recipe_run.status == RecipeRunStatus.FAILED

    def test_duration_seconds_property(self, recipe_run):
        """Test duration calculation."""
        # No duration when not started
        assert recipe_run.duration_seconds is None

        # Set start and end times
        start_time = timezone.now()
        end_time = start_time + timezone.timedelta(seconds=30)
        recipe_run.started_at = start_time
        recipe_run.completed_at = end_time
        recipe_run.save()

        assert recipe_run.duration_seconds == 30.0

    def test_current_step_property(self, recipe_run):
        """Test current step tracking."""
        # Pending run has no current step
        assert recipe_run.current_step == 0

        # Running with some results
        recipe_run.status = RecipeRunStatus.RUNNING
        recipe_run.step_results = [
            {"step_order": 1, "response": "Result 1"},
        ]
        recipe_run.save()

        # Should be on step 2 (1 complete, so next is 2)
        assert recipe_run.current_step == 2

    def test_add_step_result(self, recipe_run):
        """Test adding step results."""
        recipe_run.add_step_result(
            step_order=1,
            prompt="Show sales for North",
            response="Sales data...",
            tool_used="execute_sql",
            started_at="2024-01-15T10:00:00Z",
            completed_at="2024-01-15T10:00:05Z",
        )

        assert len(recipe_run.step_results) == 1
        result = recipe_run.step_results[0]
        assert result["step_order"] == 1
        assert result["prompt"] == "Show sales for North"
        assert result["response"] == "Sales data..."
        assert result["tool_used"] == "execute_sql"

    def test_add_multiple_step_results(self, recipe_run):
        """Test adding multiple step results."""
        recipe_run.add_step_result(
            step_order=1,
            prompt="Step 1",
            response="Result 1",
        )
        recipe_run.add_step_result(
            step_order=2,
            prompt="Step 2",
            response="Result 2",
        )

        assert len(recipe_run.step_results) == 2
        assert recipe_run.step_results[0]["step_order"] == 1
        assert recipe_run.step_results[1]["step_order"] == 2

    def test_add_step_result_with_error(self, recipe_run):
        """Test adding step result with error."""
        recipe_run.add_step_result(
            step_order=1,
            prompt="Show data",
            response="",
            error="Database connection failed",
        )

        result = recipe_run.step_results[0]
        assert result["error"] == "Database connection failed"


# ============================================================================
# 6. TestRecipeRunner (Mocked)
# ============================================================================


@pytest.mark.django_db(transaction=True)
class TestRecipeRunner:
    """Tests for the RecipeRunner async path with a provided (mocked) agent graph."""

    @pytest.mark.asyncio
    async def test_recipe_runner_validates_variables(self, recipe, user, recipe_step_1):
        """RecipeRunner.execute_async raises VariableValidationError on missing vars."""
        invalid_values = {"region": "North", "limit": 10}  # start_date missing

        runner = RecipeRunner(recipe, invalid_values, user, graph=Mock())
        with pytest.raises(VariableValidationError):
            await runner.execute_async()

    @pytest.mark.asyncio
    @patch("apps.recipes.services.runner.build_agent_graph", new_callable=AsyncMock)
    @patch("apps.recipes.services.runner.get_mcp_tools", new_callable=AsyncMock)
    @patch("apps.recipes.services.runner.get_user_oauth_tokens", new_callable=AsyncMock)
    async def test_recipe_runner_builds_headless_graph(
        self, mock_oauth, mock_mcp, mock_build, recipe, user, recipe_step_1
    ):
        """The runner must build the agent graph in HEADLESS mode (interactive=
        False), with no checkpointer, passing its job_id — so the agent gets the
        blocking materialize tool instead of the chat fire-and-ack one that would
        crash on the recipe's synthetic thread_id."""
        values = {"region": "North", "limit": 10, "start_date": "2024-01-01"}
        mock_mcp.return_value = []
        mock_oauth.return_value = {}
        stub_graph = Mock()
        stub_graph.ainvoke = AsyncMock(return_value={"messages": []})
        mock_build.return_value = stub_graph

        await RecipeRunner(recipe, values, user, job_id=123).execute_async()

        kwargs = mock_build.await_args.kwargs
        assert kwargs["interactive"] is False
        assert kwargs["job_id"] == 123
        assert kwargs["checkpointer"] is None

    @pytest.mark.asyncio
    async def test_recipe_runner_creates_run_record(self, recipe, user, recipe_step_1):
        """RecipeRunner creates a RecipeRun record."""
        values = {"region": "North", "limit": 10, "start_date": "2024-01-01"}
        mock_graph = Mock()
        mock_graph.ainvoke = AsyncMock(
            return_value={"messages": [Mock(content="Result", tool_calls=[])]}
        )

        run = await RecipeRunner(recipe, values, user, graph=mock_graph).execute_async()

        assert run is not None
        assert isinstance(run, RecipeRun)
        assert run.recipe == recipe
        assert run.variable_values == values
        assert run.run_by == user

    @pytest.mark.asyncio
    async def test_recipe_runner_executes_prompt(self, recipe, user, recipe_step_1):
        """RecipeRunner records a single executed step on success."""
        values = {"region": "West", "limit": 15, "start_date": "2024-06-01"}
        mock_graph = Mock()
        mock_graph.ainvoke = AsyncMock(
            return_value={"messages": [Mock(content="Mocked response", tool_calls=[])]}
        )

        run = await RecipeRunner(recipe, values, user, graph=mock_graph).execute_async()

        assert len(run.step_results) == 1
        assert run.step_results[0]["step_order"] == 1
        assert run.step_results[0]["success"] is True

    @pytest.mark.asyncio
    async def test_recipe_runner_substitutes_variables_in_prompts(
        self, recipe, user, recipe_step_1
    ):
        """RecipeRunner renders variable values into the prompt."""
        values = {"region": "East", "limit": 25, "start_date": "2024-03-01"}
        mock_graph = Mock()
        mock_graph.ainvoke = AsyncMock(
            return_value={"messages": [Mock(content="Mocked response", tool_calls=[])]}
        )

        run = await RecipeRunner(recipe, values, user, graph=mock_graph).execute_async()

        step_result = run.step_results[0]
        assert "East" in step_result["prompt"]
        assert "25" in step_result["prompt"]

    @pytest.mark.asyncio
    async def test_recipe_runner_handles_execution_failure(self, recipe, user, recipe_step_1):
        """RecipeRunner records a failed run when the graph raises."""
        values = {"region": "North", "limit": 10, "start_date": "2024-01-01"}
        mock_graph = Mock()
        mock_graph.ainvoke = AsyncMock(side_effect=Exception("Agent execution failed"))

        run = await RecipeRunner(recipe, values, user, graph=mock_graph).execute_async()

        assert run.status == RecipeRunStatus.FAILED
        assert len(run.step_results) > 0
        assert run.step_results[0]["success"] is False
        assert "error" in run.step_results[0]

    @pytest.mark.asyncio
    async def test_recipe_runner_updates_run_status(self, recipe, user, recipe_step_1):
        """RecipeRunner marks the run completed with a completion timestamp."""
        values = {"region": "South", "limit": 5, "start_date": "2024-02-01"}
        mock_graph = Mock()
        mock_graph.ainvoke = AsyncMock(
            return_value={"messages": [Mock(content="Success", tool_calls=[])]}
        )

        run = await RecipeRunner(recipe, values, user, graph=mock_graph).execute_async()

        assert run.status == RecipeRunStatus.COMPLETED
        assert run.completed_at is not None


# ============================================================================
# 7. TestSaveAsRecipeTool
# ============================================================================


@pytest.mark.django_db(transaction=True)
class TestSaveAsRecipeTool:
    """Tests for the save_as_recipe tool functionality."""

    @patch("apps.recipes.services.runner.build_agent_graph")
    def test_save_as_recipe_tool_exists(self, mock_build_graph, workspace, user):
        """Test that save_as_recipe tool can be created."""
        from apps.agents.tools.recipe_tool import create_recipe_tool

        tool = create_recipe_tool(workspace, user)

        assert tool is not None
        assert hasattr(tool, "name")
        assert hasattr(tool, "description")

    @patch("apps.recipes.services.runner.build_agent_graph")
    @pytest.mark.asyncio
    async def test_save_as_recipe_creates_recipe(self, mock_build_graph, workspace, user):
        """Test that save_as_recipe tool creates a recipe."""
        from apps.agents.tools.recipe_tool import create_recipe_tool

        tool = create_recipe_tool(workspace, user)

        result = await tool.ainvoke(
            {
                "name": "Customer Analysis",
                "description": "Analyze customer behavior",
                "variables": [
                    {
                        "name": "segment",
                        "type": "select",
                        "label": "Customer Segment",
                        "options": ["Premium", "Standard", "Basic"],
                    }
                ],
                "prompt": "Show {{segment}} customers",
            }
        )

        assert result["status"] == "created"
        assert "recipe_id" in result

        # Verify recipe was created
        recipe = await Recipe.objects.aget(id=result["recipe_id"])
        assert recipe.name == "Customer Analysis"
        assert len(recipe.variables) == 1
        assert recipe.prompt == "Show {{segment}} customers"

    @patch("apps.recipes.services.runner.build_agent_graph")
    @pytest.mark.asyncio
    async def test_save_as_recipe_with_prompt_and_variables(
        self, mock_build_graph, workspace, user
    ):
        """Test saving recipe with prompt template and variables."""
        from apps.agents.tools.recipe_tool import create_recipe_tool

        tool = create_recipe_tool(workspace, user)

        result = await tool.ainvoke(
            {
                "name": "Multi-Variable Analysis",
                "description": "Analysis with multiple variables",
                "variables": [
                    {"name": "year", "type": "number", "label": "Year"},
                    {"name": "region", "type": "string", "label": "Region"},
                ],
                "prompt": "Get sales for {{year}} in {{region}} and create a visualization",
            }
        )

        assert result["status"] == "created"
        recipe = await Recipe.objects.aget(id=result["recipe_id"])
        assert recipe.prompt == "Get sales for {{year}} in {{region}} and create a visualization"
        assert len(recipe.variables) == 2

    @patch("apps.recipes.services.runner.build_agent_graph")
    @pytest.mark.asyncio
    async def test_save_as_recipe_extracts_variables(self, mock_build_graph, workspace, user):
        """Test that save_as_recipe can extract variables from steps."""
        from apps.agents.tools.recipe_tool import create_recipe_tool

        tool = create_recipe_tool(workspace, user)

        # Agent should identify variables in prompt templates
        result = await tool.ainvoke(
            {
                "name": "Variable Extraction Test",
                "description": "Test variable extraction",
                "variables": [
                    {"name": "category", "type": "string", "label": "Category"},
                    {"name": "threshold", "type": "number", "label": "Threshold"},
                ],
                "prompt": "Show {{category}} with value > {{threshold}}",
            }
        )

        recipe = await Recipe.objects.aget(id=result["recipe_id"])
        variable_names = recipe.get_variable_names()
        assert "category" in variable_names
        assert "threshold" in variable_names

    @patch("apps.recipes.services.runner.build_agent_graph")
    @pytest.mark.asyncio
    async def test_save_as_recipe_sets_sharing(self, mock_build_graph, workspace, user):
        """Test that save_as_recipe can set is_shared flag."""
        from apps.agents.tools.recipe_tool import create_recipe_tool

        tool = create_recipe_tool(workspace, user)

        result = await tool.ainvoke(
            {
                "name": "Shared Recipe",
                "description": "Recipe shared with project",
                "is_shared": True,
                "variables": [],
                "prompt": "Show data",
            }
        )

        recipe = await Recipe.objects.aget(id=result["recipe_id"])
        assert recipe.is_shared is True


# ============================================================================
# 8. TestRecipeRunView
# ============================================================================


@pytest.mark.django_db(transaction=True)
class TestRecipeRunView:
    """Tests for the async recipe run endpoint."""

    @pytest.mark.asyncio
    async def test_run_endpoint_returns_201_with_real_graph(self, recipe, user, recipe_step_1):
        """POST run/ builds the real graph (LLM mocked, no MCP tools) and returns 201."""

        async def fake_ainvoke(messages, *args, **kwargs):
            return AIMessage(content="done", id="ai-1")

        mock_bound = MagicMock()
        mock_bound.ainvoke = AsyncMock(side_effect=fake_ainvoke)
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_bound

        client = AsyncClient()
        await sync_to_async(client.login)(email="test@example.com", password="testpass123")

        url = f"/api/workspaces/{recipe.workspace_id}/recipes/{recipe.id}/run/"
        body = {"variable_values": {"region": "North", "limit": 10, "start_date": "2024-01-01"}}

        with (
            patch(
                "apps.recipes.services.runner.get_mcp_tools",
                new=AsyncMock(return_value=[]),
            ),
            patch("apps.agents.graph.base.ChatAnthropic", return_value=mock_llm),
        ):
            resp = await client.post(url, data=body, content_type="application/json")

        assert resp.status_code == 201, resp.content
        data = resp.json()
        assert data["status"] == RecipeRunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_run_endpoint_forbids_non_member(self, recipe, other_user, recipe_step_1):
        """A user with no workspace membership gets 403."""
        client = AsyncClient()
        await sync_to_async(client.login)(email="other@example.com", password="otherpass123")
        url = f"/api/workspaces/{recipe.workspace_id}/recipes/{recipe.id}/run/"
        resp = await client.post(url, data={"variable_values": {}}, content_type="application/json")
        assert resp.status_code == 403
