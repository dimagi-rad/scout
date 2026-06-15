"""Tests for recipe runner module."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from asgiref.sync import async_to_sync

from apps.recipes.models import Recipe
from apps.recipes.services.runner import RecipeRunner


class TestRecipeRunner:
    """Placeholder tests for recipe runner - implement actual tests."""

    @pytest.mark.skip(reason="TODO: implement")
    def test_recipe_execution(self):
        """Test basic recipe execution."""
        pass

    @pytest.mark.skip(reason="TODO: implement")
    def test_recipe_step_sequencing(self):
        """Test that recipe steps execute in correct order."""
        pass

    @pytest.mark.skip(reason="TODO: implement")
    def test_recipe_variable_substitution(self):
        """Test variable substitution in recipe steps."""
        pass

    @pytest.mark.skip(reason="TODO: implement")
    def test_recipe_error_recovery(self):
        """Test error recovery during recipe execution."""
        pass


@pytest.mark.django_db
def test_build_graph_does_not_trigger_sync_fk_load_in_async_context(user, workspace):
    """Regression test for SCOUT-DJANGO-1P.

    ``_build_graph`` is run via ``async_to_sync`` from the sync ``execute()``
    path, so it executes inside an async event loop. The API view loads the
    recipe with ``Recipe.objects.get(pk=..., workspace=...)``, which does NOT
    cache the forward ``workspace`` FK. Accessing ``self.recipe.workspace``
    therefore triggers a synchronous lazy DB load, which Django rejects with
    ``SynchronousOnlyOperation`` inside an async context. The runner must
    reference the FK by its already-loaded ``workspace_id`` column instead.
    """
    Recipe.objects.create(
        workspace=workspace,
        name="Async FK Recipe",
        prompt="hello",
        created_by=user,
    )
    # Re-fetch so the workspace FK is NOT cached (mirrors RecipeRunView.post).
    fresh = Recipe.objects.get(name="Async FK Recipe")

    runner = RecipeRunner(recipe=fresh, variable_values={}, user=user)

    with patch(
        "apps.recipes.services.runner.build_agent_graph",
        new=AsyncMock(return_value=Mock()),
    ):
        # Must not raise SynchronousOnlyOperation.
        graph = async_to_sync(runner._build_graph)()

    assert graph is not None
