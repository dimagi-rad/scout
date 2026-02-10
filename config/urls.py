"""
URL configuration for Scout data agent platform.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.projects.views import health_check

urlpatterns = [
    path("health/", health_check, name="health_check"),
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("api/chat/", include("apps.chat.urls")),
    path("api/auth/", include("apps.chat.auth_urls")),
    path("api/projects/", include("apps.projects.urls")),
    path("api/projects/<uuid:project_id>/knowledge/", include("apps.knowledge.urls")),
    path("api/artifacts/", include("apps.artifacts.urls")),
    path(
        "api/projects/<uuid:project_id>/recipes/",
        include("apps.recipes.urls"),
    ),
    path("api/datasources/", include("apps.datasources.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
