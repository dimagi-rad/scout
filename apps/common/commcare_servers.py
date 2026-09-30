"""CommCare HQ servers Scout can read from (#719).

HQ runs as separate deployments (www, EU) with their own accounts, domains and
OAuth apps. A server is identified by a short key stored on ``Tenant.server`` and,
for CommCare connections, on ``TenantConnection.scope_key``. The empty key is the
original www server, so rows written before EU support keep their meaning unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_SERVER = ""


@dataclass(frozen=True)
class CommCareServer:
    key: str
    label: str
    base_url: str
    # The allauth provider id whose sign-in lands on this server.
    provider_id: str

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""

    @property
    def token_url(self) -> str:
        return f"{self.base_url}/oauth/token/"

    @property
    def authorize_url(self) -> str:
        return f"{self.base_url}/oauth/authorize/"

    @property
    def identity_url(self) -> str:
        return f"{self.base_url}/api/v0.5/identity/"

    @property
    def user_domains_url(self) -> str:
        return f"{self.base_url}/api/user_domains/v1/"


COMMCARE_SERVERS: dict[str, CommCareServer] = {
    server.key: server
    for server in (
        CommCareServer(DEFAULT_SERVER, "Global", "https://www.commcarehq.org", "commcare"),
        CommCareServer("eu", "EU", "https://eu.commcarehq.org", "commcare_eu"),
    )
}


class UnknownCommCareServer(ValueError):
    """A server key that names no CommCare HQ deployment Scout knows."""


def get_commcare_server(key: str | None) -> CommCareServer:
    """The server for ``key``; raises rather than guessing, so data never goes to www."""
    try:
        return COMMCARE_SERVERS[key or DEFAULT_SERVER]
    except KeyError:
        raise UnknownCommCareServer(f"Unknown CommCare HQ server {key!r}") from None


def commcare_base_url(key: str | None) -> str:
    return get_commcare_server(key).base_url


def server_for_provider(provider_id: str) -> str:
    """The server key an allauth CommCare identity signed in to.

    Matched by prefix, like ``canonical_provider``, because a deployment may
    configure the EU app under an alias id (``commcare_eu_prod``). Any other
    CommCare id, including www aliases such as ``commcare_prod``, is www.
    """
    for server in COMMCARE_SERVERS.values():
        if server.key and (
            provider_id == server.provider_id or provider_id.startswith(f"{server.provider_id}_")
        ):
            return server.key
    return DEFAULT_SERVER
