"""The Connect opportunity list is fetched once per job per credential (#918)."""

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests_mock as rm
from django.conf import settings

from apps.common.errors import ConnectAccessDeniedError
from apps.users.adapters import encrypt_credential
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.workspaces.models import WorkspaceTenant
from apps.workspaces.services import materialize
from mcp_server.loaders.connect_metadata import ConnectMetadataLoader, ConnectOpportunityLists
from mcp_server.services import materializer
from tests.pipeline_doubles import completed_pipeline_run
from tests.tenant_access import USABLE_TEST_SECRET, grant_tenant_access, record_fresh_proof

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


@pytest.fixture(autouse=True)
def _no_candidate_ddl(no_candidate_ddl):
    """Shared stub: see tests.pipeline_doubles.no_candidate_ddl."""


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


def _connect_pipeline():
    pipeline = MagicMock()
    pipeline.provider = "commcare_connect"
    pipeline.name = "connect_sync"
    pipeline.has_metadata_discovery = True
    registry = MagicMock()
    registry.list.return_value = [pipeline]
    registry.get.return_value = pipeline
    return registry


@pytest.fixture
def connect_sources(user, workspace):
    """Three Connect sources; the third signs in through a second connection."""
    tenants = [
        Tenant.objects.create(
            provider="commcare_connect", external_id=str(opp), canonical_name=f"Opp {opp}"
        )
        for opp in (901, 902, 903)
    ]
    for tenant in tenants:
        WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)
        grant_tenant_access(user, tenant)
    other = TenantConnection.objects.create(
        user=user,
        provider="commcare_connect",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential(USABLE_TEST_SECRET),
    )
    TenantMembership.objects.filter(user=user, tenant=tenants[2]).update(connection=other)
    record_fresh_proof(other, tenants[2])
    return tenants


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_job_fetches_the_list_once_per_credential(user, workspace, connect_sources):
    base = settings.CONNECT_API_URL.rstrip("/")

    async def credential_for(tm):
        return {"type": "api_key", "value": f"connection-{tm.connection_id}"}

    def fake_run_pipeline(
        tm, credential, pipeline, *, target_schema, procrastinate_job_id, **kwargs
    ):
        if tm.tenant.provider == "commcare_connect":
            materializer._run_discover_phase(
                tm, credential, pipeline, kwargs["connect_opportunity_lists"]
            )
        return completed_pipeline_run(tm, credential, pipeline, procrastinate_job_id, target_schema)

    with (
        rm.Mocker(real_http=False) as m,
        patch(
            "apps.workspaces.services.materialize.aresolve_credential", side_effect=credential_for
        ),
        patch(
            "apps.workspaces.services.materialize.get_registry", return_value=_connect_pipeline()
        ),
        patch("apps.workspaces.services.materialize.run_pipeline", side_effect=fake_run_pipeline),
        patch("apps.workspaces.services.materialize.build_and_promote_cube_schema"),
        patch("apps.workspaces.services.publication.rebuild_dependent_view_schemas", AsyncMock()),
    ):
        m.get(f"{base}/export/opp_org_program_list/", json=OPPORTUNITY_LIST)
        for opp in (901, 902, 903):
            m.get(f"{base}/export/opportunity/{opp}/", json={"id": opp})
            m.get(f"{base}/export/opportunity/{opp}/app_structure/", json={})
        await materialize.materialize_workspace_core(str(workspace.id), str(user.id), job_id=1)

    list_auth = sorted(
        r.headers["Authorization"] for r in _list_calls(m, f"{base}/export/opp_org_program_list/")
    )
    detail_calls = [r for r in m.request_history if r.path.endswith(("/901/", "/902/", "/903/"))]
    assert len(detail_calls) == 3
    assert len(list_auth) == 2
    assert len(set(list_auth)) == 2
