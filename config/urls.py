"""
URL configuration for Scout data agent platform.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path

from apps.chat.thread_views import public_thread_view
from apps.chat.urls import workspace_thread_urlpatterns
from apps.chat.views import chat_view
from apps.recipes.api.views import PublicRecipeRunView
from apps.users.auth_views import popup_complete_view
from apps.workspaces.api.workspace_views import (
    WorkspaceDetailView,
    WorkspaceListView,
    WorkspaceMemberDetailView,
    WorkspaceMemberListView,
    WorkspaceTenantView,
)
from apps.workspaces.views import health_check
from config.views import widget_js_view


def api_root(request):
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scout API</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; background: #f8fafc; color: #1e293b; }
  .card { text-align: center; max-width: 28rem; padding: 2rem; }
  h1 { font-size: 1.5rem; margin-bottom: 0.5rem; }
  p { color: #64748b; margin-bottom: 1rem; line-height: 1.5; }
  code { background: #e2e8f0; padding: 0.15rem 0.4rem; border-radius: 0.25rem;
         font-size: 0.875rem; }
  a { color: #2563eb; text-decoration: none; }
  a:hover { text-decoration: underline; }
</style>
</head>
<body>
<div class="card">
  <h1>Scout API Server</h1>
  <p>This is the API backend. If you're looking for the app, head to the frontend dev server:</p>
  <p><a href="http://localhost:5173">localhost:5173</a></p>
  <p style="font-size: 0.85rem; color: #94a3b8;">
    API endpoints live under <code>/api/</code> &middot;
    Health check at <a href="/health/">/health/</a>
  </p>
</div>
</body>
</html>"""
    return HttpResponse(html)


# All workspace-scoped content URLs, nested under /api/workspaces/<workspace_id>/
workspace_urlpatterns = [
    path("artifacts/", include("apps.artifacts.urls")),
    path("recipes/", include("apps.recipes.urls")),
    path("knowledge/", include("apps.knowledge.urls")),
    path("threads/", include((workspace_thread_urlpatterns, "chat_threads"))),
    path("", include("apps.workspaces.api.urls")),
    # Workspace management
    path("members/", WorkspaceMemberListView.as_view(), name="workspace_members"),
    path(
        "members/<int:membership_id>/",
        WorkspaceMemberDetailView.as_view(),
        name="workspace_member_detail",
    ),
    path("tenants/", WorkspaceTenantView.as_view(), name="workspace_tenants"),
    path("tenants/<uuid:wt_id>/", WorkspaceTenantView.as_view(), name="workspace_tenant_detail"),
]

urlpatterns = [
    path("", api_root, name="api_root"),
    path("widget.js", widget_js_view, name="widget-js"),
    path("health/", health_check, name="health_check"),
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("auth/popup-complete/", popup_complete_view, name="popup-complete"),
    # Workspace-scoped content APIs
    path("api/workspaces/", WorkspaceListView.as_view(), name="workspace_list"),
    path(
        "api/workspaces/<uuid:workspace_id>/",
        WorkspaceDetailView.as_view(),
        name="workspace_detail",
    ),
    path(
        "api/workspaces/<uuid:workspace_id>/",
        include(workspace_urlpatterns),
    ),
    # Chat streaming (workspace_id in body)
    path("api/chat/", chat_view, name="chat"),
    path("api/auth/", include("apps.users.auth_urls")),
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
