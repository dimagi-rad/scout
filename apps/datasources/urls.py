"""
URL configuration for datasources API.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    DatabaseConnectionViewSet,
    DataSourceCredentialViewSet,
    DataSourceTypesView,
    DataSourceViewSet,
    MaterializedDatasetViewSet,
    OAuthCallbackView,
    OAuthStartView,
    ProjectDataSourceViewSet,
    SyncJobViewSet,
)

router = DefaultRouter()
router.register(r"connections", DatabaseConnectionViewSet, basename="connection")
router.register(r"sources", DataSourceViewSet, basename="source")
router.register(r"project-sources", ProjectDataSourceViewSet, basename="project-source")
router.register(r"credentials", DataSourceCredentialViewSet, basename="credential")
router.register(r"datasets", MaterializedDatasetViewSet, basename="dataset")
router.register(r"sync-jobs", SyncJobViewSet, basename="sync-job")

urlpatterns = [
    path("", include(router.urls)),
    path("types/", DataSourceTypesView.as_view(), name="datasource-types"),
    path("oauth/start/", OAuthStartView.as_view(), name="oauth-start"),
    path("oauth/callback/", OAuthCallbackView.as_view(), name="oauth-callback"),
]
