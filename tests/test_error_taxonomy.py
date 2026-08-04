"""Shared provider errors and the Sentry filter built on them (#371, #386).

``TestBeforeSend`` is the alerting contract, so it is asserted in both
directions — what gets dropped AND what must keep coming through.
"""

import pytest

from apps.common.errors import (
    CommCareAuthError,
    ConnectAuthError,
    ExpectedStateError,
    ExpectedUpstreamError,
    OCSAuthError,
)
from apps.users.services.tenant_resolution import CommCareAuthError as resolution_commcare
from apps.users.services.tenant_resolution import ConnectAuthError as resolution_connect
from apps.users.services.tenant_resolution import OCSAuthError as resolution_ocs
from config.sentry import before_send
from mcp_server.loaders.commcare_base import CommCareAuthError as loader_commcare
from mcp_server.loaders.connect_base import ConnectAuthError as loader_connect
from mcp_server.loaders.ocs_base import OCSAuthError as loader_ocs


class TestAuthErrorConsolidation:
    """The loader and tenant-resolution modules must name the SAME class.

    Identity, not just resolvable names: two classes sharing a name means an
    ``except`` on one misses the other.
    """

    def test_ocs_auth_error_is_one_class(self):
        assert resolution_ocs is loader_ocs is OCSAuthError

    def test_commcare_auth_error_is_one_class(self):
        assert resolution_commcare is loader_commcare is CommCareAuthError

    def test_connect_auth_error_is_one_class(self):
        assert resolution_connect is loader_connect is ConnectAuthError

    def test_catching_the_loader_class_catches_the_resolver_raise(self):
        with pytest.raises(loader_ocs):
            raise resolution_ocs("raised via the tenant-resolution import path")


class TestTaxonomy:
    def test_upstream_errors_are_expected_states(self):
        assert issubclass(ExpectedUpstreamError, ExpectedStateError)

    def test_provider_defaults_to_none(self):
        assert ExpectedUpstreamError("boom").provider is None

    def test_subclasses_name_their_provider_without_a_constructor_change(self):
        """Class-attribute ``provider`` keeps ``raise Err("msg")`` call sites working."""

        class FakeProviderError(ExpectedUpstreamError):
            provider = "ocs"

        assert FakeProviderError("boom").provider == "ocs"
        assert str(FakeProviderError("boom")) == "boom"


class TestBeforeSend:
    def _hint(self, exc):
        return {"exc_info": (type(exc), exc, None)}

    def test_drops_expected_states(self):
        assert before_send({"event": 1}, self._hint(ExpectedStateError("routine"))) is None

    def test_drops_expected_upstream_states(self):
        assert before_send({"event": 1}, self._hint(ExpectedUpstreamError("routine"))) is None

    def test_keeps_unclassified_exceptions(self):
        event = {"event": 1}
        assert before_send(event, self._hint(ValueError("a real bug"))) is event

    def test_keeps_events_with_no_exception(self):
        """A bare ``logger.error`` has nothing to classify, so it is never dropped."""
        event = {"event": 1}
        assert before_send(event, {}) is event

    def test_does_not_walk_the_exception_chain(self):
        """A bug raised *while handling* an expected state is still a bug."""
        try:
            try:
                raise ExpectedStateError("routine")
            except ExpectedStateError as e:
                raise ValueError("bug in the handler") from e
        except ValueError as bug:
            event = {"event": 1}
            assert before_send(event, self._hint(bug)) is event

    @pytest.mark.parametrize(
        "auth_error",
        [CommCareAuthError, ConnectAuthError, OCSAuthError],
        ids=["commcare", "connect", "ocs"],
    )
    def test_provider_auth_errors_are_not_yet_classified(self, auth_error):
        """Scope pin: the mechanism lands here, the classification in #372."""
        event = {"event": 1}
        assert before_send(event, self._hint(auth_error("HTTP 401"))) is event
