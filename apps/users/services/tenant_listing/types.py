"""What adapters, the paginator and callers exchange; no I/O here."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

import httpx


class TenantDescriptor(NamedTuple):
    """A tenant the credential grants access to."""

    external_id: str
    canonical_name: str
    # Provider facts shown to the user as filter hints; never consulted for access.
    attributes: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ProviderRequest:
    url: str
    headers: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True)
class TenantListPage:
    tenants: tuple[TenantDescriptor, ...]
    next_url: str | None
    # Whether the page said where the list continues (null included) rather than
    # omitting the field; only verification insists on it.
    next_declared: bool = True


class TenantListError(Exception):
    """The list could not be read to its end; ``page`` is the 1-based page reached."""

    def __init__(self, message: str = "", *, page: int = 0):
        super().__init__(message or type(self).__name__)
        self.page = page


class UnsafeListingOrigin(TenantListError):
    """The configured list URL cannot safely receive the credential."""


class UnsafeNextURL(TenantListError):
    """A page pointed outside the configured origin; following it would leak the credential."""


class MalformedTenantList(TenantListError):
    """A 2xx page that is not the provider's list shape; never read it as fewer tenants."""


class PaginationCycle(TenantListError):
    pass


class PageLimitReached(TenantListError):
    pass


class ListingDeadlineExceeded(TenantListError):
    """The budget ran out before a request, or a response arrived after it."""

    def __init__(self, *, page: int, status_code: int | None = None):
        super().__init__(page=page)
        self.status_code = status_code


class RequestTimedOut(TenantListError):
    pass


class UpstreamUnreachable(TenantListError):
    """A transport failure; ``cause`` is the httpx error (its text can carry the URL)."""

    def __init__(self, cause: httpx.RequestError, *, page: int):
        super().__init__(type(cause).__name__, page=page)
        self.cause = cause


class UpstreamStatus(TenantListError):
    def __init__(self, response: httpx.Response, *, page: int):
        super().__init__(f"HTTP {response.status_code}", page=page)
        self.response = response
        self.status_code = response.status_code
