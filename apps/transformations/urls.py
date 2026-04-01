from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r"assets", views.TransformationAssetViewSet, basename="transformation-asset")
router.register(r"runs", views.TransformationRunViewSet, basename="transformation-run")

urlpatterns = [
    path("", include(router.urls)),
]
