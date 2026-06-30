from django.urls import path

from .api.views import DatasetDetailView, DatasetListView, SemanticCanvasView, SemanticQueryView

app_name = "semantic"

urlpatterns = [
    path("datasets/", DatasetListView.as_view(), name="dataset_list"),
    path("datasets/<slug:dataset_name>/", DatasetDetailView.as_view(), name="dataset_detail"),
    path("semantic-query/", SemanticQueryView.as_view(), name="semantic_query"),
    path("semantic-canvas/", SemanticCanvasView.as_view(), name="semantic_canvas"),
]
