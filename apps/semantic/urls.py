from django.urls import path

from .api.views import (
    DatasetDetailView,
    DatasetListView,
    SemanticQueryView,
    ThreadCanvasApplyView,
    ThreadCanvasCommitView,
    ThreadCanvasView,
)

app_name = "semantic"

urlpatterns = [
    path("datasets/", DatasetListView.as_view(), name="dataset_list"),
    path("datasets/<slug:dataset_name>/", DatasetDetailView.as_view(), name="dataset_detail"),
    path("semantic-query/", SemanticQueryView.as_view(), name="semantic_query"),
    path(
        "threads/<uuid:thread_id>/canvas/",
        ThreadCanvasView.as_view(),
        name="thread_canvas",
    ),
    path(
        "threads/<uuid:thread_id>/canvas/apply/",
        ThreadCanvasApplyView.as_view(),
        name="thread_canvas_apply",
    ),
    path(
        "threads/<uuid:thread_id>/canvas/commit/",
        ThreadCanvasCommitView.as_view(),
        name="thread_canvas_commit",
    ),
]
