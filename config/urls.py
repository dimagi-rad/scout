"""
URL configuration for Scout data agent platform.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.chat.views import public_thread_view
from apps.projects.api.views import RefreshSchemaView
from apps.projects.views import health_check
from apps.recipes.api.views import PublicRecipeRunView
from config.views import widget_js_view

urlpatterns = [
    path("widget.js", widget_js_view, name="widget-js"),
    path("health/", health_check, name="health_check"),
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("api/chat/", include("apps.chat.urls")),
    path("api/auth/", include("apps.chat.auth_urls")),
    path("api/artifacts/", include("apps.artifacts.urls")),
    path("api/knowledge/", include("apps.knowledge.urls")),
    path("api/recipes/", include("apps.recipes.urls")),
    path("api/data-dictionary/", include("apps.projects.api.urls")),
    path("api/refresh-schema/", RefreshSchemaView.as_view(), name="refresh_schema"),
    # Public share links (no auth required)
    path(
        "api/recipes/runs/shared/<str:share_token>/",
        PublicRecipeRunView.as_view(),
        name="public-recipe-run",
    ),
    path(
        "api/chat/threads/shared/<str:share_token>/",
        public_thread_view,
        name="public-thread",
    ),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
