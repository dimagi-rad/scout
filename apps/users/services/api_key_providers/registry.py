"""Provider-strategy registry."""

from __future__ import annotations

from apps.users.services.api_key_providers.base import CredentialProviderStrategy
from apps.users.services.api_key_providers.commcare import CommCareStrategy
from apps.users.services.api_key_providers.ocs import OCSStrategy

STRATEGIES: dict[str, type[CredentialProviderStrategy]] = {
    CommCareStrategy.provider_id: CommCareStrategy,
    OCSStrategy.provider_id: OCSStrategy,
}


def get_strategy(provider_id: str) -> type[CredentialProviderStrategy] | None:
    return STRATEGIES.get(provider_id)
