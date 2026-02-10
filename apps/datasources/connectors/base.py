"""
Abstract base class for data source connectors.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from apps.datasources.models import DataSource, DataSourceCredential


@dataclass
class TokenResult:
    """Result of OAuth token exchange or refresh."""

    access_token: str
    refresh_token: str
    expires_at: datetime
    scopes: list[str] = field(default_factory=list)
    external_user_id: str | None = None


@dataclass
class DatasetInfo:
    """Information about an available dataset."""

    name: str  # e.g., "forms", "cases"
    description: str
    estimated_rows: int | None = None


@dataclass
class SyncProgress:
    """Progress update during sync."""

    dataset: str
    fetched: int
    total: int | None
    message: str = ""


@dataclass
class SyncResult:
    """Result of a sync operation."""

    success: bool
    rows_synced: dict[str, int] = field(default_factory=dict)  # {"forms": 1500}
    error: str | None = None
    cursor: dict | None = None  # For pause/resume


# Type alias for progress callback
ProgressCallback = Callable[[SyncProgress], None]


class BaseConnector(ABC):
    """
    Abstract base for all data source connectors.

    Each connector handles:
    - OAuth authentication flow
    - Fetching data from the external API
    - Writing data to PostgreSQL schemas
    """

    def __init__(self, data_source: "DataSource"):
        self.data_source = data_source

    @property
    @abstractmethod
    def source_type(self) -> str:
        """Return the DataSourceType value this connector handles."""
        pass

    # -------------------------------------------------------------------------
    # OAuth methods
    # -------------------------------------------------------------------------

    @abstractmethod
    def get_oauth_authorization_url(
        self,
        redirect_uri: str,
        state: str,
        scopes: list[str] | None = None,
    ) -> str:
        """
        Return URL to redirect user for OAuth authorization.

        Args:
            redirect_uri: Where to redirect after authorization
            state: CSRF state token
            scopes: OAuth scopes to request (optional, uses defaults if None)

        Returns:
            Authorization URL to redirect user to
        """
        pass

    @abstractmethod
    def exchange_code_for_tokens(
        self,
        code: str,
        redirect_uri: str,
    ) -> TokenResult:
        """
        Exchange authorization code for access/refresh tokens.

        Args:
            code: Authorization code from OAuth callback
            redirect_uri: Same redirect_uri used in authorization request

        Returns:
            TokenResult with access token, refresh token, and expiry
        """
        pass

    @abstractmethod
    def refresh_access_token(self, refresh_token: str) -> TokenResult:
        """
        Refresh an expired access token.

        Args:
            refresh_token: The refresh token to use

        Returns:
            TokenResult with new access token and updated expiry
        """
        pass

    # -------------------------------------------------------------------------
    # Data discovery methods
    # -------------------------------------------------------------------------

    @abstractmethod
    def get_available_datasets(
        self,
        credential: "DataSourceCredential",
        config: dict,
    ) -> list[DatasetInfo]:
        """
        List what data can be synced (forms, cases, opportunities, etc.).

        Args:
            credential: OAuth credential to use for API calls
            config: Combined data source and project data source config

        Returns:
            List of available datasets
        """
        pass

    # -------------------------------------------------------------------------
    # Sync methods
    # -------------------------------------------------------------------------

    @abstractmethod
    def sync_dataset(
        self,
        credential: "DataSourceCredential",
        dataset_name: str,
        schema_name: str,
        config: dict,
        progress_callback: ProgressCallback | None = None,
        cursor: dict | None = None,
    ) -> SyncResult:
        """
        Fetch data and write to the specified schema.

        This method should:
        1. Create the schema if it doesn't exist
        2. Drop and recreate tables (full refresh)
        3. Fetch data from the API with pagination
        4. Write data to PostgreSQL tables
        5. Handle rate limiting by returning a cursor for resume

        Args:
            credential: OAuth credential to use for API calls
            dataset_name: Which dataset to sync (e.g., "forms")
            schema_name: PostgreSQL schema to write to
            config: Combined data source and project data source config
            progress_callback: Optional callback for progress updates
            cursor: Optional cursor to resume from (after rate limiting)

        Returns:
            SyncResult with success status, row counts, and optional cursor
        """
        pass

    # -------------------------------------------------------------------------
    # Helper methods
    # -------------------------------------------------------------------------

    def _get_headers(self, credential: "DataSourceCredential") -> dict[str, str]:
        """Get HTTP headers with authorization."""
        return {
            "Authorization": f"Bearer {credential.access_token}",
            "Content-Type": "application/json",
        }
