"""
Data source models for Scout.

Includes:
- DatabaseConnection: Centralized database credential storage
- DataSource: External API data sources (CommCare, CommCare Connect)
- ProjectDataSource: Links projects to data sources
- DataSourceCredential: OAuth tokens for API access
- MaterializedDataset: Tracks synced data in PostgreSQL schemas
- SyncJob: Individual sync operation tracking
"""
import uuid

from cryptography.fernet import Fernet
from django.conf import settings
from django.db import models


class DatabaseConnection(models.Model):
    """
    Centralized storage for database connection credentials.
    Multiple projects can reference the same connection.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, help_text="Display name, e.g. 'Production Analytics DB'")
    description = models.TextField(blank=True)

    # Connection details
    db_host = models.CharField(max_length=255)
    db_port = models.IntegerField(default=5432)
    db_name = models.CharField(max_length=255)

    # Encrypted credentials
    _db_user = models.BinaryField(db_column="db_user")
    _db_password = models.BinaryField(db_column="db_password")

    # Metadata
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_database_connections",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        permissions = [
            ("manage_database_connections", "Can create and edit database connections"),
        ]

    def __str__(self):
        return self.name

    def _get_fernet(self):
        """Get Fernet instance for encryption/decryption."""
        key = settings.DB_CREDENTIAL_KEY
        if not key:
            raise ValueError("DB_CREDENTIAL_KEY is not set in settings")
        return Fernet(key.encode() if isinstance(key, str) else key)

    @property
    def db_user(self):
        """Decrypt and return the database username."""
        if not self._db_user:
            return ""
        f = self._get_fernet()
        return f.decrypt(bytes(self._db_user)).decode()

    @db_user.setter
    def db_user(self, value):
        """Encrypt and store the database username."""
        if not value:
            self._db_user = b""
            return
        f = self._get_fernet()
        self._db_user = f.encrypt(value.encode())

    @property
    def db_password(self):
        """Decrypt and return the database password."""
        if not self._db_password:
            return ""
        f = self._get_fernet()
        return f.decrypt(bytes(self._db_password)).decode()

    @db_password.setter
    def db_password(self, value):
        """Encrypt and store the database password."""
        if not value:
            self._db_password = b""
            return
        f = self._get_fernet()
        self._db_password = f.encrypt(value.encode())

    def get_connection_params(self, schema: str = "public", timeout_seconds: int = 30) -> dict:
        """Return connection params for psycopg2/SQLAlchemy."""
        return {
            "host": self.db_host,
            "port": self.db_port,
            "dbname": self.db_name,
            "user": self.db_user,
            "password": self.db_password,
            "options": f"-c search_path={schema},public -c statement_timeout={timeout_seconds * 1000}",
        }


class DataSourceType(models.TextChoices):
    """Supported external data source types."""

    COMMCARE = "commcare", "CommCare"
    COMMCARE_CONNECT = "commcare_connect", "CommCare Connect"


class DataSource(models.Model):
    """
    A configured external data source (e.g., CommCare production instance).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, help_text="Display name, e.g. 'CommCare Production'")
    source_type = models.CharField(max_length=50, choices=DataSourceType.choices)
    base_url = models.URLField(help_text="Base API URL, e.g. 'https://www.commcarehq.org'")

    # Source-specific configuration
    # CommCare: {"domain": "my-project", "app_id": "abc123"}
    # Connect: {"org_slug": "my-org"}
    config = models.JSONField(default=dict, blank=True)

    # OAuth client credentials (for this Scout instance)
    oauth_client_id = models.CharField(max_length=255, blank=True)
    _oauth_client_secret = models.BinaryField(db_column="oauth_client_secret", null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_data_sources",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.get_source_type_display()})"

    def _get_fernet(self):
        """Get Fernet instance for encryption/decryption."""
        key = settings.DB_CREDENTIAL_KEY
        if not key:
            raise ValueError("DB_CREDENTIAL_KEY is not set in settings")
        return Fernet(key.encode() if isinstance(key, str) else key)

    @property
    def oauth_client_secret(self):
        """Decrypt and return the OAuth client secret."""
        if not self._oauth_client_secret:
            return ""
        f = self._get_fernet()
        return f.decrypt(bytes(self._oauth_client_secret)).decode()

    @oauth_client_secret.setter
    def oauth_client_secret(self, value):
        """Encrypt and store the OAuth client secret."""
        if not value:
            self._oauth_client_secret = None
            return
        f = self._get_fernet()
        self._oauth_client_secret = f.encrypt(value.encode())


class CredentialMode(models.TextChoices):
    """How credentials are managed for a project data source."""

    PROJECT = "project", "Project-level"  # Service credentials, shared data
    USER = "user", "User-level"  # Each user authenticates, isolated data


class ProjectDataSource(models.Model):
    """
    Links a project to a data source with sync configuration.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="project_data_sources",
    )
    data_source = models.ForeignKey(
        DataSource,
        on_delete=models.CASCADE,
        related_name="project_links",
    )

    credential_mode = models.CharField(
        max_length=20,
        choices=CredentialMode.choices,
        default=CredentialMode.USER,
    )

    # What to sync
    # CommCare: {"datasets": ["forms"], "form_xmlns": ["http://..."]}
    # Connect: {"datasets": ["opportunities", "visits"]}
    sync_config = models.JSONField(default=dict, blank=True)

    refresh_interval_hours = models.IntegerField(default=24)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["project", "data_source"]
        verbose_name = "Project Data Source"
        verbose_name_plural = "Project Data Sources"

    def __str__(self):
        return f"{self.project.name} - {self.data_source.name}"


class DataSourceCredential(models.Model):
    """
    OAuth tokens for accessing a data source.
    Either project-level (shared) or user-level (individual).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    data_source = models.ForeignKey(
        DataSource,
        on_delete=models.CASCADE,
        related_name="credentials",
    )

    # One of these must be set (enforced by constraint)
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="data_source_credentials",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="data_source_credentials",
    )

    # Encrypted tokens
    _access_token = models.BinaryField(db_column="access_token")
    _refresh_token = models.BinaryField(db_column="refresh_token")
    token_expires_at = models.DateTimeField()

    # OAuth metadata
    scopes = models.JSONField(default=list, blank=True)
    external_user_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="User ID in the external system",
    )

    # Status
    is_valid = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(project__isnull=False, user__isnull=True)
                    | models.Q(project__isnull=True, user__isnull=False)
                ),
                name="credential_project_xor_user",
            ),
        ]
        # Note: unique_together with nullable fields doesn't work as expected in Django,
        # so we handle uniqueness in save() or use partial indexes in migration
        verbose_name = "Data Source Credential"
        verbose_name_plural = "Data Source Credentials"

    def __str__(self):
        owner = self.project.name if self.project else self.user.email
        return f"{self.data_source.name} - {owner}"

    def _get_fernet(self):
        """Get Fernet instance for encryption/decryption."""
        key = settings.DB_CREDENTIAL_KEY
        if not key:
            raise ValueError("DB_CREDENTIAL_KEY is not set in settings")
        return Fernet(key.encode() if isinstance(key, str) else key)

    @property
    def access_token(self):
        """Decrypt and return the access token."""
        if not self._access_token:
            return ""
        f = self._get_fernet()
        return f.decrypt(bytes(self._access_token)).decode()

    @access_token.setter
    def access_token(self, value):
        """Encrypt and store the access token."""
        if not value:
            self._access_token = b""
            return
        f = self._get_fernet()
        self._access_token = f.encrypt(value.encode())

    @property
    def refresh_token(self):
        """Decrypt and return the refresh token."""
        if not self._refresh_token:
            return ""
        f = self._get_fernet()
        return f.decrypt(bytes(self._refresh_token)).decode()

    @refresh_token.setter
    def refresh_token(self, value):
        """Encrypt and store the refresh token."""
        if not value:
            self._refresh_token = b""
            return
        f = self._get_fernet()
        self._refresh_token = f.encrypt(value.encode())


class DatasetStatus(models.TextChoices):
    """Status of a materialized dataset."""

    PENDING = "pending", "Pending"
    SYNCING = "syncing", "Syncing"
    READY = "ready", "Ready"
    ERROR = "error", "Error"
    EXPIRED = "expired", "Expired"


class MaterializedDataset(models.Model):
    """
    Tracks a materialized dataset in a PostgreSQL schema.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project_data_source = models.ForeignKey(
        ProjectDataSource,
        on_delete=models.CASCADE,
        related_name="materialized_datasets",
    )

    # Set for user-level credential mode, null for project-level
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="materialized_datasets",
    )

    schema_name = models.CharField(
        max_length=63,
        help_text="PostgreSQL schema name (max 63 chars)",
    )
    status = models.CharField(
        max_length=20,
        choices=DatasetStatus.choices,
        default=DatasetStatus.PENDING,
    )

    # Sync tracking
    last_sync_at = models.DateTimeField(null=True, blank=True)
    next_sync_at = models.DateTimeField(null=True, blank=True)
    sync_error = models.TextField(blank=True)
    row_counts = models.JSONField(
        default=dict,
        blank=True,
        help_text='Row counts per table, e.g. {"forms": 1500, "cases": 300}',
    )

    # Activity tracking for cleanup
    last_activity_at = models.DateTimeField(auto_now_add=True)

    # Sync progress tracking for pause/resume
    sync_cursor = models.JSONField(
        default=dict,
        blank=True,
        help_text="Cursor for resuming interrupted syncs",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["project_data_source", "user"]
        verbose_name = "Materialized Dataset"
        verbose_name_plural = "Materialized Datasets"

    def __str__(self):
        owner = self.user.email if self.user else "project"
        return f"{self.project_data_source} ({owner}) - {self.schema_name}"


class SyncJobStatus(models.TextChoices):
    """Status of a sync job."""

    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    PAUSED = "paused", "Paused"  # Rate limited
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class SyncJob(models.Model):
    """
    Tracks individual sync operations for debugging and progress.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    materialized_dataset = models.ForeignKey(
        MaterializedDataset,
        on_delete=models.CASCADE,
        related_name="sync_jobs",
    )

    status = models.CharField(
        max_length=20,
        choices=SyncJobStatus.choices,
        default=SyncJobStatus.QUEUED,
    )

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Progress tracking
    progress = models.JSONField(
        default=dict,
        blank=True,
        help_text='Progress per dataset, e.g. {"forms": {"fetched": 500, "total": 1500}}',
    )

    # For pause/resume on rate limits
    resume_after = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When to resume after rate limiting",
    )

    error_message = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Sync Job"
        verbose_name_plural = "Sync Jobs"

    def __str__(self):
        return f"{self.materialized_dataset} - {self.status} ({self.created_at})"
