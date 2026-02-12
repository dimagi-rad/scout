"""URL configuration for chat app (streaming endpoint + thread history)."""
from django.urls import path

from apps.chat.views import chat_view, thread_list_view, thread_messages_view

app_name = "chat"

urlpatterns = [
    path("", chat_view, name="chat"),
    path("threads/", thread_list_view, name="thread_list"),
    path("threads/<uuid:thread_id>/messages/", thread_messages_view, name="thread_messages"),
]
