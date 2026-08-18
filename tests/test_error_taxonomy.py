"""Shared provider errors and the Sentry filter built on them (#371, #386).

``TestBeforeSend`` is the alerting contract, so it is asserted in both
directions — what gets dropped AND what must keep coming through.
"""

import pytest

from apps.common.errors import (
    CommCareAccessDeniedError,
    CommCareAuthError,
    CommCareTokenExpiredError,
    ConnectAccessDeniedError,
    ConnectAuthError,
    ConnectTokenExpiredError,
    ExpectedStateError,
    ExpectedUpstreamError,
    OCSAccessDeniedError,
    OCSAuthError,
    OCSTokenExpiredError,
    UpstreamAccessDenied,
    UpstreamTokenExpired,
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
    def test_provider_base_classes_are_not_classified(self, auth_error):
        """The bare provider classes must keep reaching Sentry.

        This is the guard on #371's precondition, and it is deliberate rather
        than incidental. ``tenant_resolution`` raises these bare from the login
        signal, whose only failure signal today IS the Sentry event — the user
        just gets an empty data-sources page. Classifying the base would silence
        that path without replacing it, so expectedness lives on the leaves.
        """
        event = {"event": 1}
        assert before_send(event, self._hint(auth_error("HTTP 401"))) is event

    @pytest.mark.parametrize(
        "leaf",
        [
            CommCareTokenExpiredError,
            CommCareAccessDeniedError,
            ConnectTokenExpiredError,
            ConnectAccessDeniedError,
            OCSTokenExpiredError,
            OCSAccessDeniedError,
        ],
    )
    def test_loader_leaf_classes_are_dropped(self, leaf):
        """The loader path is classified: it reaches the user via the chat summary."""
        assert before_send({"event": 1}, self._hint(leaf("upstream said no"))) is None


class TestAuthErrorAxes:
    """Both axes must be catchable: by provider, and by cause."""

    @pytest.mark.parametrize(
        ("base", "expired", "denied"),
        [
            (CommCareAuthError, CommCareTokenExpiredError, CommCareAccessDeniedError),
            (ConnectAuthError, ConnectTokenExpiredError, ConnectAccessDeniedError),
            (OCSAuthError, OCSTokenExpiredError, OCSAccessDeniedError),
        ],
        ids=["commcare", "connect", "ocs"],
    )
    def test_provider_base_catches_both_causes(self, base, expired, denied):
        """Existing `except <Provider>AuthError` keeps working after the split."""
        assert issubclass(expired, base)
        assert issubclass(denied, base)

    @pytest.mark.parametrize(
        "denied",
        [CommCareAccessDeniedError, ConnectAccessDeniedError, OCSAccessDeniedError],
        ids=["commcare", "connect", "ocs"],
    )
    def test_access_denied_is_catchable_across_providers(self, denied):
        """The hook the revocation work (#378/#384) needs: one except for every 403."""
        assert issubclass(denied, UpstreamAccessDenied)

    @pytest.mark.parametrize(
        "expired",
        [CommCareTokenExpiredError, ConnectTokenExpiredError, OCSTokenExpiredError],
        ids=["commcare", "connect", "ocs"],
    )
    def test_expiry_is_catchable_across_providers(self, expired):
        assert issubclass(expired, UpstreamTokenExpired)

    def test_the_two_causes_do_not_catch_each_other(self):
        """The whole point of #372 — a 403 must not be handled as an expiry."""
        assert not issubclass(OCSAccessDeniedError, UpstreamTokenExpired)
        assert not issubclass(OCSTokenExpiredError, UpstreamAccessDenied)

    def test_provider_is_reported(self):
        assert OCSAccessDeniedError("x").provider == "ocs"
        assert CommCareTokenExpiredError("x").provider == "commcare"
        assert ConnectAccessDeniedError("x").provider == "commcare_connect"
