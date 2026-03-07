"""Tests for recipe soft delete functionality."""

import pytest

from apps.recipes.models import Recipe


@pytest.fixture
def recipe(db, user, workspace):
    return Recipe.objects.create(
        workspace=workspace,
        name="Test Recipe",
        created_by=user,
    )


@pytest.mark.django_db
def test_soft_delete_sets_is_deleted(recipe):
    recipe.soft_delete(deleted_by=recipe.created_by)
    recipe.refresh_from_db()
    assert recipe.is_deleted is True
    assert recipe.deleted_at is not None
    assert recipe.deleted_by == recipe.created_by


@pytest.mark.django_db
def test_soft_deleted_recipe_hidden_from_default_queryset(recipe):
    recipe.soft_delete(deleted_by=recipe.created_by)
    assert Recipe.objects.filter(id=recipe.id).count() == 0


@pytest.mark.django_db
def test_soft_deleted_recipe_visible_via_all_objects(recipe):
    recipe.soft_delete(deleted_by=recipe.created_by)
    assert Recipe.all_objects.filter(id=recipe.id).count() == 1


@pytest.mark.django_db
def test_undelete_restores_recipe(recipe):
    recipe.soft_delete(deleted_by=recipe.created_by)
    recipe.undelete()
    recipe.refresh_from_db()
    assert recipe.is_deleted is False
