from django.urls import path

from apps.memory.views import personal_memory_detail_view, personal_memory_list_view

app_name = "memory"

urlpatterns = [
    path("personal/", personal_memory_list_view, name="personal_list"),
    path("personal/<uuid:memory_id>/", personal_memory_detail_view, name="personal_detail"),
]
