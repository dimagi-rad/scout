"""Connecting OCS teams by slug, singly or chained, from the ``teams`` claim (OCS #4685)."""

from __future__ import annotations

from apps.users.providers.ocs.provider import OCSProvider


class TestClaims:
    def test_teams_scope_is_requested(self):
        assert "teams" in OCSProvider.get_default_scope(None)
