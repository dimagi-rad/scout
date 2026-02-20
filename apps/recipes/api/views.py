"""
API views for recipe management.

Provides a public endpoint for accessing shared recipe runs.
Project-scoped recipe CRUD views have been removed (replaced by workspace-scoped APIs).
"""

from django.shortcuts import get_object_or_404
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.recipes.models import RecipeRun

from .serializers import PublicRecipeRunSerializer


class PublicRecipeRunView(APIView):
    """Public access to a shared recipe run."""

    permission_classes = [AllowAny]
    authentication_classes = []
    renderer_classes = [JSONRenderer]

    def get(self, request, share_token):
        run = get_object_or_404(
            RecipeRun,
            share_token=share_token,
            is_public=True,
        )
        serializer = PublicRecipeRunSerializer(run)
        return Response(serializer.data)
