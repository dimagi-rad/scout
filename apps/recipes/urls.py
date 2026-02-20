"""
URL configuration for recipes app.
"""

from django.urls import path

from .api.views import (
    RecipeDetailView,
    RecipeListView,
    RecipeRunDetailView,
    RecipeRunListView,
    RecipeRunView,
)

app_name = "recipes"

urlpatterns = [
    path("", RecipeListView.as_view(), name="list"),
    path("<uuid:recipe_id>/", RecipeDetailView.as_view(), name="detail"),
    path("<uuid:recipe_id>/run/", RecipeRunView.as_view(), name="run"),
    path("<uuid:recipe_id>/runs/", RecipeRunListView.as_view(), name="runs"),
    path("<uuid:recipe_id>/runs/<uuid:run_id>/", RecipeRunDetailView.as_view(), name="run_detail"),
]
