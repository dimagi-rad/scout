"""Keep authenticated provider requests within their configured origin."""

from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests


class UnsafeProviderURL(ValueError):
    """A provider URL cannot safely receive this loader's credentials."""


def _check_reference(url: str) -> None:
    if not isinstance(url, str) or not url or any(c.isspace() or ord(c) < 32 for c in url):
        raise UnsafeProviderURL("Invalid provider origin URL")
    if "\\" in url:
        raise UnsafeProviderURL("Invalid provider origin URL")
    try:
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None:
            raise ValueError
        if parts.scheme and (parts.scheme not in ("http", "https") or not parts.hostname):
            raise ValueError
        if url.startswith("//") and not parts.hostname:
            raise ValueError
        if parts.port == 0:
            raise ValueError
    except ValueError:
        raise UnsafeProviderURL("Invalid provider origin URL") from None


def _canonical_url(url: str) -> str:
    _check_reference(url)
    try:
        return requests.Request("GET", url).prepare().url
    except (requests.RequestException, ValueError):
        raise UnsafeProviderURL("Invalid provider origin URL") from None


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


class ProviderURLPolicy:
    def __init__(self, base_url: str) -> None:
        self.base_url = _canonical_url(base_url)
        self.origin = _origin(self.base_url)
        scheme, host, _ = self.origin
        if scheme == "http":
            try:
                loopback = ip_address(host).is_loopback
            except ValueError:
                loopback = host == "localhost"
            if not loopback:
                raise UnsafeProviderURL("Provider origin must use HTTPS")

    def resolve(self, url: str, *, relative_to: str | None = None) -> str:
        _check_reference(url)
        target = _canonical_url(urljoin(relative_to or self.base_url, url))
        scheme, host, port = _origin(target)
        # Connect can emit HTTP pagination links behind its HTTPS reverse proxy.
        if self.origin == ("https", host, 443) and (scheme, port) == ("http", 80):
            parts = urlsplit(target)
            netloc = f"[{host}]" if ":" in host else host
            target = urlunsplit(parts._replace(scheme="https", netloc=netloc))
        if _origin(target) != self.origin:
            raise UnsafeProviderURL("Provider URL is outside the configured origin")
        return target
