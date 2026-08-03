"""Shared provider errors and the Sentry filter built on them (#371, #386)."""

import pytest

from apps.common.errors import CommCareAuthError, ConnectAuthError, OCSAuthError


class TestAuthErrorConsolidation:
    """The loader and tenant-resolution modules must name the SAME class.

    These assert *identity*, not that both names resolve — two classes sharing a
    name is exactly the bug (an ``except`` on one misses the other).
    """

    def test_ocs_auth_error_is_one_class(self):
        from apps.users.services.tenant_resolution import OCSAuthError as resolution_cls
        from mcp_server.loaders.ocs_base import OCSAuthError as loader_cls

        assert resolution_cls is loader_cls is OCSAuthError

    def test_commcare_auth_error_is_one_class(self):
        from apps.users.services.tenant_resolution import CommCareAuthError as resolution_cls
        from mcp_server.loaders.commcare_base import CommCareAuthError as loader_cls

        assert resolution_cls is loader_cls is CommCareAuthError

    def test_connect_auth_error_is_one_class(self):
        from apps.users.services.tenant_resolution import ConnectAuthError as resolution_cls
        from mcp_server.loaders.connect_base import ConnectAuthError as loader_cls

        assert resolution_cls is loader_cls is ConnectAuthError

    def test_catching_the_loader_class_catches_the_resolver_raise(self):
        """The bug this consolidation fixes, stated as behaviour."""
        from apps.users.services.tenant_resolution import OCSAuthError as resolution_cls
        from mcp_server.loaders.ocs_base import OCSAuthError as loader_cls

        with pytest.raises(loader_cls):
            raise resolution_cls("raised via the tenant-resolution import path")
