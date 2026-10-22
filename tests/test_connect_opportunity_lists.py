"""The Connect opportunity list is fetched once per job per credential (#918)."""

import threading

import pytest
import requests_mock as rm

from apps.common.errors import ConnectAccessDeniedError
from mcp_server.loaders.connect_metadata import ConnectMetadataLoader, ConnectOpportunityLists
from mcp_server.services import materializer

BASE = "https://connect.example.com"
LIST_URL = f"{BASE}/export/opp_org_program_list/"
OPPORTUNITY_LIST = {
    "organizations": [{"id": 1, "slug": "dimagi", "name": "Dimagi"}],
    "programs": [{"id": 25, "name": "CHC Nutrition", "organization": "dimagi"}],
    "opportunities": [
        {"id": 814, "name": "CHC Nutrition", "visit_count": 1200},
        {"id": 815, "name": "CHC Nutrition 2", "visit_count": 300},
    ],
}


def _loader(opportunity_id, *, token="tok", lists=None, key=None):
    return ConnectMetadataLoader(
        opportunity_id=opportunity_id,
        credential={"type": "api_key", "value": token},
        base_url=BASE,
        opportunity_lists=lists,
        credential_key=key,
    )


def _mock_opportunity(m, opportunity_id):
    m.get(f"{BASE}/export/opportunity/{opportunity_id}/", json={"id": opportunity_id})
    m.get(
        f"{BASE}/export/opportunity/{opportunity_id}/app_structure/",
        json={"learn_app": None, "deliver_app": None},
    )


def _list_calls(m, url=LIST_URL):
    return [r for r in m.request_history if r.url == url]


def test_loads_under_one_credential_share_one_list_fetch():
    lists = ConnectOpportunityLists()
    with rm.Mocker() as m:
        m.get(LIST_URL, json=OPPORTUNITY_LIST)
        _mock_opportunity(m, 814)
        _mock_opportunity(m, 815)
        shared = [_loader(opp, lists=lists, key=7).load() for opp in (814, 815)]
        unshared = [_loader(opp).load() for opp in (814, 815)]

    assert len(_list_calls(m)) == 3
    assert shared == unshared
    assert materializer._connect_source_totals(shared[1], 815) == {"visits": 300}


def test_different_credentials_never_share_a_list():
    lists = ConnectOpportunityLists()
    with rm.Mocker() as m:
        m.get(
            LIST_URL,
            [
                {"json": {"opportunities": [{"id": 814, "visit_count": 1}]}},
                {"json": {"opportunities": [{"id": 815, "visit_count": 2}]}},
            ],
        )
        _mock_opportunity(m, 814)
        _mock_opportunity(m, 815)
        first = _loader(814, token="alice", lists=lists, key=1).load()
        second = _loader(815, token="bob", lists=lists, key=2).load()

    calls = _list_calls(m)
    assert [c.headers["Authorization"] for c in calls] == ["Bearer alice", "Bearer bob"]
    assert first["all_opportunities"] == [{"id": 814, "visit_count": 1}]
    assert second["all_opportunities"] == [{"id": 815, "visit_count": 2}]


def test_a_loader_without_a_credential_key_does_not_share():
    lists = ConnectOpportunityLists()
    with rm.Mocker() as m:
        m.get(LIST_URL, json=OPPORTUNITY_LIST)
        _mock_opportunity(m, 814)
        _loader(814, lists=lists).load()
        _loader(814, lists=lists).load()

    assert len(_list_calls(m)) == 2


@pytest.mark.parametrize("failure", [{"status_code": 503}, {"status_code": 404}, {"text": "<"}])
def test_an_unreadable_list_leaves_only_progress_totals_missing(failure):
    lists = ConnectOpportunityLists()
    with rm.Mocker() as m:
        m.get(LIST_URL, **failure)
        _mock_opportunity(m, 814)
        _mock_opportunity(m, 815)
        results = [_loader(opp, lists=lists, key=7).load() for opp in (814, 815)]

    # Shared, so the job's other sources do not repeat a failing fetch.
    assert len(_list_calls(m)) == 1
    for result, opp in zip(results, (814, 815), strict=True):
        assert result["opportunity"] == {"id": opp}
        assert result["all_opportunities"] == []
        assert materializer._connect_source_totals(result, opp) == {}


def test_a_list_denial_still_fails_the_load_and_is_not_shared():
    lists = ConnectOpportunityLists()
    with rm.Mocker() as m:
        m.get(LIST_URL, status_code=403)
        for _ in range(2):
            with pytest.raises(ConnectAccessDeniedError) as caught:
                _loader(814, lists=lists, key=7).load()
            assert caught.value.denial_scope == "unknown"

    assert len(_list_calls(m)) == 2


def test_concurrent_loads_wait_for_the_first_fetch():
    lists = ConnectOpportunityLists()
    fetching = threading.Event()
    release = threading.Event()
    calls = []

    def fetch():
        calls.append(1)
        fetching.set()
        assert release.wait(5)
        return OPPORTUNITY_LIST

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(lists.get("key", fetch))) for _ in range(3)
    ]
    threads[0].start()
    assert fetching.wait(5)
    for thread in threads[1:]:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(5)

    assert len(calls) == 1
    assert results == [OPPORTUNITY_LIST] * 3
