"""
Celery configuration for Scout.

This module configures Celery for background task processing.
"""

import os

from celery import Celery
from celery.contrib.django.task import DjangoTask

# Set the default Django settings module
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

app = Celery("scout", task_cls=DjangoTask)

# Load config from Django settings with CELERY_ prefix
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks in all registered Django apps
app.autodiscover_tasks()

app.conf.beat_schedule = {}
app.conf.timezone = "UTC"


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Debug task to verify Celery is working."""
    print(f"Request: {self.request!r}")
