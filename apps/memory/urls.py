from django.urls import path

from apps.memory.views import personal_memory_detail_view, personal_memory_list_view
from apps.memory.workspace_views import workspace_memory_detail_view, workspace_memory_list_view

app_name = "memory"

urlpatterns = [
    path("personal/", personal_memory_list_view, name="personal_list"),
    path("personal/<uuid:memory_id>/", personal_memory_detail_view, name="personal_detail"),
]

# Nested under /api/workspaces/<workspace_id>/memory/ by config/urls.py.
workspace_memory_urlpatterns = [
    path("", workspace_memory_list_view, name="workspace_list"),
    path("<uuid:memory_id>/", workspace_memory_detail_view, name="workspace_detail"),
]
