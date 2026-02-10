"""
Celery configuration for Scout.

This module configures Celery for background task processing,
including periodic tasks for data sync and cleanup.
"""
import os

from celery import Celery
from celery.schedules import crontab

# Set the default Django settings module
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

app = Celery("scout")

# Load config from Django settings with CELERY_ prefix
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks in all registered Django apps
app.autodiscover_tasks()

# Celery Beat schedule for periodic tasks
app.conf.beat_schedule = {
    # Schedule dataset refreshes - runs every hour
    "schedule-dataset-refreshes": {
        "task": "apps.datasources.tasks.schedule_dataset_refreshes",
        "schedule": crontab(minute=0),  # Every hour at :00
    },
    # Cleanup inactive datasets - runs every hour at :15
    "cleanup-inactive-datasets": {
        "task": "apps.datasources.tasks.cleanup_inactive_datasets",
        "schedule": crontab(minute=15),  # Every hour at :15
    },
    # Refresh expiring OAuth tokens - runs every 15 minutes
    "refresh-expiring-tokens": {
        "task": "apps.datasources.tasks.refresh_expiring_tokens",
        "schedule": crontab(minute="*/15"),  # Every 15 minutes
    },
    # Resume paused sync jobs - runs every 5 minutes
    "resume-paused-syncs": {
        "task": "apps.datasources.tasks.resume_paused_syncs",
        "schedule": crontab(minute="*/5"),  # Every 5 minutes
    },
}

app.conf.timezone = "UTC"


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Debug task to verify Celery is working."""
    print(f"Request: {self.request!r}")
