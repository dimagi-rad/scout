"""
CommCare data source connector.

Handles OAuth authentication and data sync from CommCare HQ.
"""
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx
from django.db import connection

from apps.datasources.models import DataSourceType

from .base import BaseConnector, DatasetInfo, ProgressCallback, SyncProgress, SyncResult, TokenResult
from .registry import register_connector

if TYPE_CHECKING:
    from apps.datasources.models import DataSourceCredential

logger = logging.getLogger(__name__)

# CommCare OAuth endpoints
COMMCARE_AUTH_URL = "https://www.commcarehq.org/oauth/authorize/"
COMMCARE_TOKEN_URL = "https://www.commcarehq.org/oauth/token/"

# Default scopes for CommCare
DEFAULT_SCOPES = ["access_apis"]

# Rate limit handling
RATE_LIMIT_STATUS = 429
RATE_LIMIT_RETRY_AFTER_DEFAULT = 60  # seconds


@register_connector(DataSourceType.COMMCARE)
class CommCareConnector(BaseConnector):
    """
    Connector for CommCare HQ data.

    Supports syncing:
    - Forms (form submissions with JSON data)
    - Cases (case data) - future
    - Users (mobile workers) - future
    """

    @property
    def source_type(self) -> str:
        return DataSourceType.COMMCARE

    # -------------------------------------------------------------------------
    # OAuth methods
    # -------------------------------------------------------------------------

    def get_oauth_authorization_url(
        self,
        redirect_uri: str,
        state: str,
        scopes: list[str] | None = None,
    ) -> str:
        """Return CommCare OAuth authorization URL."""
        params = {
            "response_type": "code",
            "client_id": self.data_source.oauth_client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(scopes or DEFAULT_SCOPES),
        }
        return f"{COMMCARE_AUTH_URL}?{urlencode(params)}"

    def exchange_code_for_tokens(
        self,
        code: str,
        redirect_uri: str,
    ) -> TokenResult:
        """Exchange authorization code for tokens."""
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.data_source.oauth_client_id,
            "client_secret": self.data_source.oauth_client_secret,
        }

        with httpx.Client() as client:
            response = client.post(
                COMMCARE_TOKEN_URL,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            token_data = response.json()

        expires_in = token_data.get("expires_in", 3600)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        return TokenResult(
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token", ""),
            expires_at=expires_at,
            scopes=token_data.get("scope", "").split(),
        )

    def refresh_access_token(self, refresh_token: str) -> TokenResult:
        """Refresh an expired access token."""
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.data_source.oauth_client_id,
            "client_secret": self.data_source.oauth_client_secret,
        }

        with httpx.Client() as client:
            response = client.post(
                COMMCARE_TOKEN_URL,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            token_data = response.json()

        expires_in = token_data.get("expires_in", 3600)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        return TokenResult(
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token", refresh_token),
            expires_at=expires_at,
            scopes=token_data.get("scope", "").split(),
        )

    # -------------------------------------------------------------------------
    # Data discovery methods
    # -------------------------------------------------------------------------

    def get_available_datasets(
        self,
        credential: "DataSourceCredential",
        config: dict,
    ) -> list[DatasetInfo]:
        """List available datasets (forms, cases, etc.)."""
        # For now, we support forms
        # Future: query CommCare API to discover available apps/forms
        datasets = [
            DatasetInfo(
                name="forms",
                description="Form submissions from CommCare",
                estimated_rows=None,
            ),
        ]

        # Future datasets:
        # - cases: Case data
        # - users: Mobile worker data

        return datasets

    # -------------------------------------------------------------------------
    # Sync methods
    # -------------------------------------------------------------------------

    def sync_dataset(
        self,
        credential: "DataSourceCredential",
        dataset_name: str,
        schema_name: str,
        config: dict,
        progress_callback: ProgressCallback | None = None,
        cursor: dict | None = None,
    ) -> SyncResult:
        """Sync data from CommCare to PostgreSQL."""
        if dataset_name == "forms":
            return self._sync_forms(
                credential=credential,
                schema_name=schema_name,
                config=config,
                progress_callback=progress_callback,
                cursor=cursor,
            )
        else:
            return SyncResult(
                success=False,
                error=f"Unknown dataset: {dataset_name}",
            )

    def _sync_forms(
        self,
        credential: "DataSourceCredential",
        schema_name: str,
        config: dict,
        progress_callback: ProgressCallback | None = None,
        cursor: dict | None = None,
    ) -> SyncResult:
        """Sync form submissions from CommCare."""
        domain = config.get("domain")
        if not domain:
            return SyncResult(success=False, error="domain is required in config")

        # Get form xmlns filters (optional)
        form_xmlns_list = config.get("form_xmlns", [])
        app_id = config.get("app_id")

        try:
            # Create schema if it doesn't exist
            self._create_schema(schema_name)

            # Drop and recreate forms table (full refresh)
            self._create_forms_table(schema_name)

            # Determine starting offset from cursor
            offset = cursor.get("offset", 0) if cursor else 0
            total_synced = cursor.get("total_synced", 0) if cursor else 0

            # Fetch and insert forms
            limit = 100  # Page size
            total_count = None

            with httpx.Client(timeout=60.0) as client:
                while True:
                    # Build API URL
                    url = self._build_forms_url(domain, app_id, form_xmlns_list, offset, limit)

                    # Make API request
                    try:
                        response = client.get(url, headers=self._get_headers(credential))
                    except httpx.TimeoutException:
                        logger.warning(f"Timeout fetching forms at offset {offset}, retrying...")
                        time.sleep(5)
                        continue

                    # Handle rate limiting
                    if response.status_code == RATE_LIMIT_STATUS:
                        retry_after = int(
                            response.headers.get("Retry-After", RATE_LIMIT_RETRY_AFTER_DEFAULT)
                        )
                        logger.info(f"Rate limited, need to wait {retry_after}s")
                        return SyncResult(
                            success=False,
                            rows_synced={"forms": total_synced},
                            error=f"Rate limited, retry after {retry_after}s",
                            cursor={
                                "offset": offset,
                                "total_synced": total_synced,
                                "retry_after": retry_after,
                            },
                        )

                    response.raise_for_status()
                    data = response.json()

                    # Get total count from first response
                    if total_count is None:
                        meta = data.get("meta", {})
                        total_count = meta.get("total_count") or meta.get("total", 0)

                    # Process forms
                    forms = data.get("objects", [])
                    if not forms:
                        break

                    # Insert forms into database
                    self._insert_forms(schema_name, forms)
                    total_synced += len(forms)

                    # Report progress
                    if progress_callback:
                        progress_callback(
                            SyncProgress(
                                dataset="forms",
                                fetched=total_synced,
                                total=total_count,
                                message=f"Fetched {total_synced}/{total_count or '?'} forms",
                            )
                        )

                    # Check if we've fetched all
                    if len(forms) < limit:
                        break

                    offset += limit

                    # Small delay to be nice to the API
                    time.sleep(0.5)

            logger.info(f"Synced {total_synced} forms to {schema_name}")
            return SyncResult(
                success=True,
                rows_synced={"forms": total_synced},
            )

        except httpx.HTTPStatusError as e:
            logger.exception(f"HTTP error syncing forms: {e}")
            return SyncResult(
                success=False,
                rows_synced={"forms": total_synced if "total_synced" in dir() else 0},
                error=f"HTTP {e.response.status_code}: {e.response.text[:200]}",
            )
        except Exception as e:
            logger.exception(f"Error syncing forms: {e}")
            return SyncResult(
                success=False,
                error=str(e),
            )

    def _build_forms_url(
        self,
        domain: str,
        app_id: str | None,
        form_xmlns_list: list[str],
        offset: int,
        limit: int,
    ) -> str:
        """Build CommCare forms API URL."""
        base_url = self.data_source.base_url.rstrip("/")
        url = f"{base_url}/a/{domain}/api/form/v1/"

        params = {"offset": offset, "limit": limit}
        if app_id:
            params["app_id"] = app_id
        if form_xmlns_list:
            # CommCare accepts multiple xmlns params
            params["xmlns"] = form_xmlns_list[0]  # For now, just use first one

        return f"{url}?{urlencode(params)}"

    # -------------------------------------------------------------------------
    # Database methods
    # -------------------------------------------------------------------------

    def _create_schema(self, schema_name: str) -> None:
        """Create PostgreSQL schema if it doesn't exist."""
        with connection.cursor() as cursor:
            # Use quote_ident equivalent for safety
            cursor.execute(
                f"CREATE SCHEMA IF NOT EXISTS {self._quote_ident(schema_name)}"
            )

    def _create_forms_table(self, schema_name: str) -> None:
        """Create or recreate the forms table."""
        table_name = f"{self._quote_ident(schema_name)}.forms"

        with connection.cursor() as cursor:
            # Drop existing table
            cursor.execute(f"DROP TABLE IF EXISTS {table_name}")

            # Create table with JSON storage (schema follows data)
            cursor.execute(f"""
                CREATE TABLE {table_name} (
                    id SERIAL PRIMARY KEY,
                    instance_id TEXT UNIQUE NOT NULL,
                    domain TEXT,
                    app_id TEXT,
                    xmlns TEXT,
                    case_id TEXT,
                    user_id TEXT,
                    username TEXT,
                    received_on TIMESTAMPTZ,
                    form_data JSONB,
                    metadata JSONB,
                    synced_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # Create indexes for common queries
            cursor.execute(
                f"CREATE INDEX ON {table_name} (received_on)"
            )
            cursor.execute(
                f"CREATE INDEX ON {table_name} (xmlns)"
            )
            cursor.execute(
                f"CREATE INDEX ON {table_name} (user_id)"
            )
            cursor.execute(
                f"CREATE INDEX ON {table_name} USING GIN (form_data)"
            )

    def _insert_forms(self, schema_name: str, forms: list[dict[str, Any]]) -> None:
        """Insert form records into the database."""
        if not forms:
            return

        table_name = f"{self._quote_ident(schema_name)}.forms"

        with connection.cursor() as cursor:
            for form in forms:
                form_obj = form.get("form", {})
                metadata = form.get("metadata", {}) or form_obj.get("meta", {})

                # Extract key fields
                instance_id = (
                    metadata.get("instanceID")
                    or form_obj.get("meta", {}).get("instanceID")
                    or form.get("id")
                )
                if not instance_id:
                    continue

                case_id = (
                    form_obj.get("case", {}).get("@case_id")
                    or form_obj.get("case", {}).get("case_id")
                )

                cursor.execute(
                    f"""
                    INSERT INTO {table_name} (
                        instance_id, domain, app_id, xmlns, case_id,
                        user_id, username, received_on, form_data, metadata
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (instance_id) DO UPDATE SET
                        form_data = EXCLUDED.form_data,
                        metadata = EXCLUDED.metadata,
                        synced_at = NOW()
                    """,
                    [
                        instance_id,
                        form.get("domain"),
                        form.get("app_id"),
                        form.get("xmlns"),
                        case_id,
                        metadata.get("userID"),
                        metadata.get("username"),
                        form.get("received_on"),
                        json.dumps(form_obj) if form_obj else None,
                        json.dumps(metadata) if metadata else None,
                    ],
                )

    def _quote_ident(self, identifier: str) -> str:
        """
        Quote a PostgreSQL identifier to prevent SQL injection.
        Only allows alphanumeric and underscore characters.
        """
        # Validate identifier contains only safe characters
        if not identifier.replace("_", "").isalnum():
            raise ValueError(f"Invalid identifier: {identifier}")
        return f'"{identifier}"'
