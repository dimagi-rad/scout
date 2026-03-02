"""
URL configuration for data dictionary endpoints.
"""

from django.urls import path

from .views import DataDictionaryView, TableDetailView

app_name = "data_dictionary"

urlpatterns = [
    path("", DataDictionaryView.as_view(), name="data_dictionary"),
    path("tables/<str:qualified_name>/", TableDetailView.as_view(), name="table_detail"),
]
