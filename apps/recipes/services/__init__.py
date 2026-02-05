"""
Recipe services for the Scout data agent platform.

This module provides services for executing recipe workflows.
"""

from apps.recipes.services.runner import RecipeRunner

__all__ = ["RecipeRunner"]
