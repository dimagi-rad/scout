"""
Celery tasks for data source sync and maintenance.
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.db import connection, transaction
from django.utils import timezone

from .connectors import get_connector
from .connectors.base import SyncProgress
from .models import (
    CredentialMode,
    DatasetStatus,
    DataSourceCredential,
    MaterializedDataset,
    SyncJob,
    SyncJobStatus,
)

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def sync_dataset(self, materialized_dataset_id: str):
    """
    Sync data from external API to PostgreSQL schema.

    This task:
    1. Gets the appropriate credential (project or user level)
    2. Creates a SyncJob for tracking
    3. Calls the connector's sync method
    4. Updates dataset status based on result
    """
    try:
        dataset = MaterializedDataset.objects.select_related(
            "project_data_source__data_source",
            "project_data_source__project",
            "user",
        ).get(id=materialized_dataset_id)
    except MaterializedDataset.DoesNotExist:
        logger.error(f"MaterializedDataset {materialized_dataset_id} not found")
        return

    pds = dataset.project_data_source

    # Get the appropriate credential
    try:
        if pds.credential_mode == CredentialMode.PROJECT:
            credential = DataSourceCredential.objects.get(
                data_source=pds.data_source,
                project=pds.project,
            )
        else:
            if not dataset.user:
                logger.error(f"User-level dataset {dataset.id} has no user")
                return
            credential = DataSourceCredential.objects.get(
                data_source=pds.data_source,
                user=dataset.user,
            )
    except DataSourceCredential.DoesNotExist:
        logger.error(f"No credential found for dataset {dataset.id}")
        dataset.status = DatasetStatus.ERROR
        dataset.sync_error = "No valid credential found"
        dataset.save()
        return

    if not credential.is_valid:
        logger.error(f"Credential {credential.id} is invalid")
        dataset.status = DatasetStatus.ERROR
        dataset.sync_error = "Credential is invalid - re-authorization required"
        dataset.save()
        return

    # Get connector
    connector = get_connector(pds.data_source)

    # Create sync job
    job = SyncJob.objects.create(
        materialized_dataset=dataset,
        status=SyncJobStatus.RUNNING,
        started_at=timezone.now(),
    )

    # Update dataset status
    dataset.status = DatasetStatus.SYNCING
    dataset.save()

    try:
        # Build config from data source and project data source
        config = {**pds.data_source.config, **pds.sync_config}

        # Get datasets to sync (default to all available if not specified)
        datasets_to_sync = config.get("datasets", ["forms"])

        total_rows = {}

        for dataset_name in datasets_to_sync:
            # Define progress callback
            def progress_callback(progress: SyncProgress):
                job.progress[progress.dataset] = {
                    "fetched": progress.fetched,
                    "total": progress.total,
                    "message": progress.message,
                }
                job.save(update_fields=["progress"])

            # Run sync with resume cursor if available
            result = connector.sync_dataset(
                credential=credential,
                dataset_name=dataset_name,
                schema_name=dataset.schema_name,
                config=config,
                progress_callback=progress_callback,
                cursor=dataset.sync_cursor.get(dataset_name) if dataset.sync_cursor else None,
            )

            if not result.success:
                # Check if this is a rate limit pause
                if result.cursor and result.cursor.get("retry_after"):
                    # Save cursor for resume
                    if not dataset.sync_cursor:
                        dataset.sync_cursor = {}
                    dataset.sync_cursor[dataset_name] = result.cursor

                    job.status = SyncJobStatus.PAUSED
                    job.resume_after = timezone.now() + timedelta(
                        seconds=result.cursor["retry_after"]
                    )
                    job.save()

                    dataset.save()
                    logger.info(
                        f"Sync paused for dataset {dataset.id}, "
                        f"will resume after {job.resume_after}"
                    )
                    return

                # Actual error
                raise Exception(result.error)

            total_rows.update(result.rows_synced)

        # Success - update dataset
        dataset.status = DatasetStatus.READY
        dataset.last_sync_at = timezone.now()
        dataset.next_sync_at = timezone.now() + timedelta(hours=pds.refresh_interval_hours)
        dataset.row_counts = total_rows
        dataset.sync_error = ""
        dataset.sync_cursor = {}  # Clear cursor on success
        dataset.save()

        job.status = SyncJobStatus.COMPLETED
        job.completed_at = timezone.now()
        job.save()

        # Update credential last used
        credential.last_used_at = timezone.now()
        credential.save(update_fields=["last_used_at"])

        logger.info(f"Sync completed for dataset {dataset.id}: {total_rows}")

    except Exception as e:
        logger.exception(f"Sync failed for dataset {dataset.id}: {e}")

        dataset.status = DatasetStatus.ERROR
        dataset.sync_error = str(e)[:500]  # Truncate long errors
        dataset.save()

        job.status = SyncJobStatus.FAILED
        job.error_message = str(e)[:1000]
        job.completed_at = timezone.now()
        job.save()

        # Retry with exponential backoff for transient errors
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))


@shared_task
def schedule_dataset_refreshes():
    """
    Periodic task to queue sync for datasets that are due for refresh.
    Runs every hour.
    """
    now = timezone.now()

    due_datasets = MaterializedDataset.objects.filter(
        status=DatasetStatus.READY,
        next_sync_at__lte=now,
        project_data_source__is_active=True,
    ).values_list("id", flat=True)

    count = 0
    for dataset_id in due_datasets:
        sync_dataset.delay(str(dataset_id))
        count += 1

    if count:
        logger.info(f"Scheduled {count} dataset refreshes")


@shared_task
def cleanup_inactive_datasets():
    """
    Periodic task to drop schemas for inactive datasets.
    Datasets inactive for >24 hours are marked as expired.
    Runs every hour.
    """
    cutoff = timezone.now() - timedelta(hours=24)

    inactive = MaterializedDataset.objects.filter(
        status=DatasetStatus.READY,
        last_activity_at__lt=cutoff,
    )

    count = 0
    for dataset in inactive:
        try:
            _drop_schema(dataset.schema_name)
            dataset.status = DatasetStatus.EXPIRED
            dataset.save()
            count += 1
            logger.info(f"Expired inactive dataset {dataset.id} ({dataset.schema_name})")
        except Exception as e:
            logger.exception(f"Failed to cleanup dataset {dataset.id}: {e}")

    if count:
        logger.info(f"Cleaned up {count} inactive datasets")


@shared_task
def refresh_expiring_tokens():
    """
    Periodic task to refresh OAuth tokens that are expiring soon.
    Runs every 15 minutes.
    """
    # Refresh tokens expiring in the next hour
    expiring_soon = timezone.now() + timedelta(hours=1)

    credentials = DataSourceCredential.objects.filter(
        is_valid=True,
        token_expires_at__lte=expiring_soon,
    ).select_related("data_source")

    count = 0
    for credential in credentials:
        try:
            connector = get_connector(credential.data_source)
            result = connector.refresh_access_token(credential.refresh_token)

            credential.access_token = result.access_token
            credential.refresh_token = result.refresh_token
            credential.token_expires_at = result.expires_at
            credential.save()
            count += 1
            logger.info(f"Refreshed token for credential {credential.id}")

        except Exception as e:
            logger.exception(f"Failed to refresh token for credential {credential.id}: {e}")
            credential.is_valid = False
            credential.save(update_fields=["is_valid"])
            # TODO: Notify user/admin that re-authorization is needed

    if count:
        logger.info(f"Refreshed {count} expiring tokens")


@shared_task
def resume_paused_syncs():
    """
    Periodic task to resume sync jobs that were paused due to rate limiting.
    Runs every 5 minutes.
    """
    now = timezone.now()

    paused_jobs = SyncJob.objects.filter(
        status=SyncJobStatus.PAUSED,
        resume_after__lte=now,
    ).select_related("materialized_dataset")

    count = 0
    for job in paused_jobs:
        # Queue a new sync task (it will resume from cursor)
        sync_dataset.delay(str(job.materialized_dataset_id))
        count += 1
        logger.info(f"Resumed paused sync for job {job.id}")

    if count:
        logger.info(f"Resumed {count} paused syncs")


def _drop_schema(schema_name: str) -> None:
    """Drop a PostgreSQL schema and all its contents."""
    # Validate schema name to prevent SQL injection
    if not schema_name.replace("_", "").isalnum():
        raise ValueError(f"Invalid schema name: {schema_name}")

    with connection.cursor() as cursor:
        cursor.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
