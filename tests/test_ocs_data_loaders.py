"""Tests for OCS data loaders."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mcp_server.loaders.ocs_experiments import OCSExperimentLoader
from mcp_server.loaders.ocs_messages import OCSMessageLoader
from mcp_server.loaders.ocs_participants import OCSParticipantLoader
from mcp_server.loaders.ocs_sessions import OCSSessionLoader

CREDENTIAL = {"type": "oauth", "value": "tok"}
BASE_URL = "https://ocs.example"


def test_experiment_loader_fetches_single_detail():
    loader = OCSExperimentLoader(experiment_id="exp-1", credential=CREDENTIAL, base_url=BASE_URL)
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "id": "exp-1",
        "name": "Onboarding Bot",
        "url": "https://ocs.example/api/experiments/exp-1/",
        "version_number": 3,
    }
    with patch.object(loader._session, "get", return_value=resp):
        pages = list(loader.load_pages())
        assert pages == [
            (
                [
                    {
                        "experiment_id": "exp-1",
                        "name": "Onboarding Bot",
                        "url": "https://ocs.example/api/experiments/exp-1/",
                        "version_number": 3,
                    }
                ],
                1,
            )
        ]


def test_session_loader_paginates_and_filters_by_experiment():
    loader = OCSSessionLoader(experiment_id="exp-1", credential=CREDENTIAL, base_url=BASE_URL)
    page = MagicMock(status_code=200)
    page.json.return_value = {
        "results": [
            {
                "id": "sess-1",
                "experiment": "exp-1",
                "participant": {"identifier": "p1", "platform": "web", "remote_id": "r1"},
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T01:00:00Z",
                "tags": ["a", "b"],
            }
        ],
        "next": None,
    }
    with patch.object(loader._session, "get", return_value=page):
        rows = [r for pg, _ in loader.load_pages() for r in pg]
        assert rows == [
            {
                "session_id": "sess-1",
                "experiment_id": "exp-1",
                "participant_identifier": "p1",
                "participant_platform": "web",
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T01:00:00Z",
                "tags": ["a", "b"],
            }
        ]


def test_session_loader_extracts_experiment_id_from_nested_object():
    """OCS list endpoint returns 'experiment' as a nested object — extract the id."""
    loader = OCSSessionLoader(experiment_id="exp-1", credential=CREDENTIAL, base_url=BASE_URL)
    page = MagicMock(status_code=200)
    page.json.return_value = {
        "results": [
            {
                "id": "sess-1",
                "experiment": {
                    "id": "exp-1",
                    "name": "Test Bot",
                    "url": "https://x/api/experiments/exp-1/",
                    "version_number": 3,
                },
                "participant": {"identifier": "p1", "platform": "", "remote_id": ""},
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T01:00:00Z",
                "tags": [],
            }
        ],
        "next": None,
    }
    with patch.object(loader._session, "get", return_value=page):
        rows = [r for pg, _ in loader.load_pages() for r in pg]
        assert rows[0]["experiment_id"] == "exp-1"


def test_message_loader_flattens_messages_with_composite_pk():
    loader = OCSMessageLoader(experiment_id="exp-1", credential=CREDENTIAL, base_url=BASE_URL)
    sessions_page = MagicMock(status_code=200)
    sessions_page.json.return_value = {
        "results": [{"id": "sess-1", "experiment": "exp-1"}],
        "next": None,
    }
    detail = MagicMock(status_code=200)
    detail.json.return_value = {
        "id": "sess-1",
        "messages": [
            {
                "role": "user",
                "content": "hi",
                "created_at": "2026-04-01T00:00:00Z",
                "metadata": {"k": "v"},
                "tags": [],
            },
            {
                "role": "assistant",
                "content": "hello",
                "created_at": "2026-04-01T00:00:01Z",
                "metadata": {},
                "tags": ["greeting"],
            },
        ],
    }
    with patch.object(loader._session, "get", side_effect=[sessions_page, detail]):
        pages = list(loader.load_pages())
        rows = [r for pg, _ in pages for r in pg]
        assert rows == [
            {
                "message_id": "sess-1:0",
                "session_id": "sess-1",
                "message_index": 0,
                "role": "user",
                "content": "hi",
                "created_at": "2026-04-01T00:00:00Z",
                "metadata": {"k": "v"},
                "tags": [],
            },
            {
                "message_id": "sess-1:1",
                "session_id": "sess-1",
                "message_index": 1,
                "role": "assistant",
                "content": "hello",
                "created_at": "2026-04-01T00:00:01Z",
                "metadata": {},
                "tags": ["greeting"],
            },
        ]
        # Every per-session tuple carries the session count as its total —
        # message progress is denominated in sessions (issue #221).
        assert [total for _, total in pages] == [1]


def test_message_loader_indexes_sessions_before_fetching_details():
    """Two-pass: the full session list is walked first, then one tuple is
    yielded per session — including sessions with no messages — each carrying
    the total session count so the writer can report determinate progress."""
    loader = OCSMessageLoader(experiment_id="exp-1", credential=CREDENTIAL, base_url=BASE_URL)
    list_page1 = MagicMock(status_code=200)
    list_page1.json.return_value = {
        "results": [{"id": "sess-1"}],
        "next": f"{BASE_URL}/api/sessions/?cursor=abc",
    }
    list_page2 = MagicMock(status_code=200)
    list_page2.json.return_value = {
        "results": [{"id": "sess-2"}],
        "next": None,
    }
    detail_1 = MagicMock(status_code=200)
    detail_1.json.return_value = {
        "id": "sess-1",
        "messages": [{"role": "user", "content": "hi"}],
    }
    detail_2 = MagicMock(status_code=200)
    detail_2.json.return_value = {"id": "sess-2", "messages": []}

    with patch.object(
        loader._session, "get", side_effect=[list_page1, list_page2, detail_1, detail_2]
    ) as mock_get:
        pages = list(loader.load_pages())

    # Both list pages are fetched before the first session detail.
    urls = [call.args[0] for call in mock_get.call_args_list]
    assert urls == [
        f"{BASE_URL}/api/sessions/",
        f"{BASE_URL}/api/sessions/?cursor=abc",
        f"{BASE_URL}/api/sessions/sess-1/",
        f"{BASE_URL}/api/sessions/sess-2/",
    ]
    # One tuple per session, each with total_sessions=2; the empty session
    # still yields so the writer can advance the progress bar.
    assert len(pages) == 2
    assert [total for _, total in pages] == [2, 2]
    assert len(pages[0][0]) == 1
    assert pages[1][0] == []


def test_participant_loader_maps_dedicated_endpoint_fields():
    """Uses the dedicated /api/participants endpoint with rich fields + custom data."""
    loader = OCSParticipantLoader(experiment_id="exp-1", credential=CREDENTIAL, base_url=BASE_URL)
    page = MagicMock(status_code=200)
    page.json.return_value = {
        "results": [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "identifier": "p1",
                "name": "John",
                "platform": "api",
                "remote_id": "r1",
                "data": [
                    {
                        "chatbot": "Support Bot",
                        "chatbot_id": "exp-1",
                        "data": {"name": "John", "timezone": "Africa/Johannesburg"},
                    }
                ],
            },
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "identifier": "p2",
                "name": "",
                "platform": "whatsapp",
                "remote_id": "",
                "data": [],
            },
        ],
        "next": None,
    }
    with patch.object(loader._session, "get", return_value=page):
        rows = [r for pg, _ in loader.load_pages() for r in pg]
        assert rows == [
            {
                "participant_id": "11111111-1111-1111-1111-111111111111",
                "identifier": "p1",
                "name": "John",
                "platform": "api",
                "remote_id": "r1",
                "data": [
                    {
                        "chatbot": "Support Bot",
                        "chatbot_id": "exp-1",
                        "data": {"name": "John", "timezone": "Africa/Johannesburg"},
                    }
                ],
            },
            {
                "participant_id": "22222222-2222-2222-2222-222222222222",
                "identifier": "p2",
                "name": "",
                "platform": "whatsapp",
                "remote_id": "",
                "data": [],
            },
        ]


def test_participant_loader_queries_chatbot_scoped_endpoint():
    """The loader hits /api/participants filtered by the tenant's chatbot."""
    loader = OCSParticipantLoader(experiment_id="exp-1", credential=CREDENTIAL, base_url=BASE_URL)
    page = MagicMock(status_code=200)
    page.json.return_value = {"results": [], "next": None}
    with patch.object(loader._session, "get", return_value=page) as mock_get:
        list(loader.load_pages())
    args, kwargs = mock_get.call_args
    assert args[0] == f"{BASE_URL}/api/participants"
    assert kwargs["params"] == {"chatbot": "exp-1"}
