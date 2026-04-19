"""Tests for OCSMetadataLoader."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mcp_server.loaders.ocs_metadata import OCSMetadataLoader


def test_metadata_loader_returns_experiment_detail():
    loader = OCSMetadataLoader(
        experiment_id="exp-1",
        credential={"type": "oauth", "value": "tok"},
        base_url="https://ocs.example",
    )
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "id": "exp-1",
        "name": "Onboarding Bot",
        "url": "https://ocs.example/api/experiments/exp-1/",
        "version_number": 2,
    }
    with patch.object(loader._session, "get", return_value=resp):
        md = loader.load()
    assert md["experiment"]["name"] == "Onboarding Bot"
    assert md["experiment"]["id"] == "exp-1"
