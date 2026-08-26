"""URL configuration for chat app (streaming endpoint + thread history).

Thread-related endpoints are nested under /api/workspaces/<workspace_id>/threads/.
The chat streaming endpoint lives at /api/chat/ (workspace_id in request body).
"""

from django.urls import path

from apps.chat.thread_views import (
    thread_artifacts_view,
    thread_detail_view,
    thread_list_view,
    thread_messages_view,
    thread_share_view,
    thread_viewed_view,
)
from apps.chat.views import chat_view

app_name = "chat"

# Thread endpoints nested under workspace (included via config/urls.py workspace router)
workspace_thread_urlpatterns = [
    path("", thread_list_view, name="thread_list"),
    path("<uuid:thread_id>/", thread_detail_view, name="thread_detail"),
    path("<uuid:thread_id>/artifacts/", thread_artifacts_view, name="thread_artifacts"),
    path("<uuid:thread_id>/messages/", thread_messages_view, name="thread_messages"),
    path("<uuid:thread_id>/share/", thread_share_view, name="thread_share"),
    path("<uuid:thread_id>/viewed/", thread_viewed_view, name="thread_viewed"),
]

urlpatterns = [
    path("", chat_view, name="chat"),
]
