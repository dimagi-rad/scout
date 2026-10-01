"""OCS session platform comes from the session record, not the nested participant."""

from unittest.mock import MagicMock, patch

from mcp_server.loaders.ocs_sessions import OCSSessionLoader

CREDENTIAL = {"type": "oauth", "value": "tok"}
BASE_URL = "https://ocs.example"

# Shape of GET /api/sessions/ items from open-chat-studio's ExperimentSessionSerializer:
# the nested participant has only identifier/remote_id, ``platform`` is a session field.
OCS_SESSION_LIST_ITEM = {
    "url": "https://ocs.example/api/sessions/sess-1/",
    "id": "sess-1",
    "team": {"name": "Team", "slug": "team"},
    "experiment": {
        "id": "exp-1",
        "name": "Bot",
        "url": "https://ocs.example/api/experiments/exp-1/",
        "version_number": 2,
    },
    "participant": {"identifier": "+2557000000", "remote_id": ""},
    "created_at": "2026-04-01T00:00:00Z",
    "updated_at": "2026-04-01T01:00:00Z",
    "status": "active",
    "platform": "telegram",
    "tags": [],
    "state": {},
}


def test_session_loader_reads_platform_from_session_not_participant():
    loader = OCSSessionLoader(experiment_id="exp-1", credential=CREDENTIAL, base_url=BASE_URL)
    page = MagicMock(status_code=200)
    page.json.return_value = {"results": [OCS_SESSION_LIST_ITEM], "next": None}
    with patch.object(loader._session, "get", return_value=page):
        rows = loader.load()
    assert rows[0]["participant_platform"] == "telegram"
    assert rows[0]["participant_identifier"] == "+2557000000"


def test_session_loader_platform_defaults_to_empty_when_absent():
    loader = OCSSessionLoader(experiment_id="exp-1", credential=CREDENTIAL, base_url=BASE_URL)
    item = {k: v for k, v in OCS_SESSION_LIST_ITEM.items() if k != "platform"}
    page = MagicMock(status_code=200)
    page.json.return_value = {"results": [item], "next": None}
    with patch.object(loader._session, "get", return_value=page):
        assert loader.load()[0]["participant_platform"] == ""
