"""Metadata loader for CommCare Connect.

Fetches opportunity detail and org/program structure from the Connect API.
This metadata is stored in TenantMetadata for reference.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Hashable

from apps.common.errors import (
    DenialScope,
    ExpectedUpstreamError,
    TokenRefreshError,
    UpstreamAccessDenied,
    UpstreamUnavailable,
)
from mcp_server.loaders.commcare_metadata import _extract_case_types, _extract_form_definitions
from mcp_server.loaders.connect_base import ConnectBaseLoader

logger = logging.getLogger(__name__)


class ConnectOpportunityLists:
    """One load job's ``/export/opp_org_program_list/`` responses, by credential.

    The list is user-global (every opportunity the credential can see), so the
    job's Connect sources would otherwise each re-fetch the same, slow export
    (#918). Concurrent loads under one key wait for the first fetch instead of
    racing it. Keys must identify the credential: one user's list must never
    answer for another's. Create one per job, never per process.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key_locks: dict[Hashable, threading.Lock] = {}
        self._lists: dict[Hashable, dict] = {}

    def get(self, key: Hashable, fetch: Callable[[], dict]) -> dict:
        """Return the list remembered for ``key``, fetching it on first use.

        A raised error is not remembered, so the next load under ``key`` fetches
        again, as it did before the list was shared. During a token-refresh
        outage, each queued load therefore waits out its own failed refresh;
        that load would fail on its next request anyway, and re-raising one
        load's exception in another would report the wrong source.
        """
        with self._lock:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            if key not in self._lists:
                self._lists[key] = fetch()
            return self._lists[key]


class ConnectMetadataLoader(ConnectBaseLoader):
    """Fetch metadata for a Connect opportunity."""

    def __init__(
        self,
        opportunity_id: int,
        credential: dict[str, str],
        base_url: str | None = None,
        *,
        opportunity_lists: ConnectOpportunityLists | None = None,
        credential_key: Hashable | None = None,
    ) -> None:
        super().__init__(opportunity_id, credential, base_url)
        # Sharing needs both: without a key, the credential behind the list is unknown.
        self._opportunity_lists = opportunity_lists if credential_key is not None else None
        self._credential_key = credential_key

    def load(self) -> dict:
        org_data = self._fetch_org_data()
        opp_detail = self._fetch_opportunity_detail()
        form_definitions: dict = {}
        case_types: list = []
        try:
            app_structure = self._fetch_app_structure()
            # Real Connect returns {"learn_app": <HQ app JSON|null>, "deliver_app": ...}.
            # Each app is HQ application JSON; reuse the CommCare extractors verbatim.
            apps = [
                a for a in (app_structure.get("deliver_app"), app_structure.get("learn_app")) if a
            ]
            form_definitions = _extract_form_definitions(apps)
            case_types = _extract_case_types(apps)
        except UpstreamUnavailable as e:
            logger.warning(
                "app_structure unavailable for opportunity %s; continuing without form_definitions: %s",
                self.opportunity_id,
                e,
            )
        except (ExpectedUpstreamError, TokenRefreshError):
            raise
        except Exception:
            logger.exception(
                "Failed to fetch app_structure for opportunity %s; continuing without form_definitions",
                self.opportunity_id,
            )

        logger.info(
            "Loaded metadata for Connect opportunity %s: %s (%d forms)",
            self.opportunity_id,
            opp_detail.get("name", "unknown"),
            len(form_definitions),
        )
        return {
            "opportunity": opp_detail,
            "organizations": org_data.get("organizations", []),
            "programs": org_data.get("programs", []),
            "all_opportunities": org_data.get("opportunities", []),
            "form_definitions": form_definitions,
            "case_types": case_types,
        }

    def _fetch_org_data(self) -> dict:
        if self._opportunity_lists is None:
            return self._fetch_org_data_or_empty()
        return self._opportunity_lists.get(
            (self._credential_key, self.base_url), self._fetch_org_data_or_empty
        )

    def _fetch_org_data_or_empty(self) -> dict:
        """Return the user-global opportunity list, or ``{}`` when it cannot be read.

        It only supplies progress denominators (``visit_count``), so an outage
        must not fail the load; a shared ``{}`` also spares the job's other
        sources the same retries. Auth failures still raise and are not shared.
        """
        try:
            return self._fetch_org_data_uncached()
        except UpstreamUnavailable as e:
            logger.warning(
                "opp_org_program_list unavailable; continuing without progress totals: %s", e
            )
        except (ExpectedUpstreamError, TokenRefreshError):
            raise
        except Exception:
            logger.exception(
                "Failed to fetch opp_org_program_list; continuing without progress totals"
            )
        return {}

    def _fetch_org_data_uncached(self) -> dict:
        url = f"{self.base_url}/export/opp_org_program_list/"
        try:
            return self._get(url).json()
        except UpstreamAccessDenied as exc:
            # This global endpoint does not establish which opportunity was
            # denied. Do not revoke the opportunity currently being loaded.
            exc.denial_scope = DenialScope.UNKNOWN
            raise

    def _fetch_opportunity_detail(self) -> dict:
        url = f"{self.base_url}/export/opportunity/{self.opportunity_id}/"
        return self._get(url).json()

    def _fetch_app_structure(self) -> dict:
        url = f"{self.base_url}/export/opportunity/{self.opportunity_id}/app_structure/"
        return self._get(url, params={"app_type": "both"}).json()
