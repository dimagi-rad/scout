import pytest
import requests_mock as rm

from mcp_server.loaders.connect_base import ConnectAuthError, ConnectBaseLoader


@pytest.fixture
def loader():
    return ConnectBaseLoader(
        opportunity_id=814,
        credential={"type": "oauth", "value": "test-token-123"},
        base_url="https://connect.example.com",
    )


class TestConnectBaseLoader:
    def test_get_json(self, loader):
        with rm.Mocker() as m:
            m.get(
                "https://connect.example.com/export/opportunity/814/",
                json={"id": 814, "name": "Test Opp"},
                status_code=200,
            )
            resp = loader._get("https://connect.example.com/export/opportunity/814/")
            assert resp.json()["id"] == 814

    def test_get_csv(self, loader):
        csv_content = "id,username,visit_date\n1,alice,2025-01-01\n2,bob,2025-01-02\n"
        with rm.Mocker() as m:
            m.get(
                "https://connect.example.com/export/opportunity/814/user_visits/",
                text=csv_content,
                headers={"Content-Type": "text/csv"},
                status_code=200,
            )
            rows = loader._get_csv(
                "https://connect.example.com/export/opportunity/814/user_visits/"
            )
            assert len(rows) == 2
            assert rows[0]["username"] == "alice"

    def test_auth_error_on_401(self, loader):
        with rm.Mocker() as m:
            m.get(
                "https://connect.example.com/export/opportunity/814/",
                status_code=401,
            )
            with pytest.raises(ConnectAuthError):
                loader._get("https://connect.example.com/export/opportunity/814/")

    def test_auth_error_on_403(self, loader):
        with rm.Mocker() as m:
            m.get(
                "https://connect.example.com/export/opportunity/814/",
                status_code=403,
            )
            with pytest.raises(ConnectAuthError):
                loader._get("https://connect.example.com/export/opportunity/814/")

    def test_bearer_token_header(self, loader):
        assert loader._session.headers["Authorization"] == "Bearer test-token-123"

    def test_get_csv_empty(self, loader):
        with rm.Mocker() as m:
            m.get(
                "https://connect.example.com/export/opportunity/814/user_data/",
                text="username,name,phone\n",
                headers={"Content-Type": "text/csv"},
                status_code=200,
            )
            rows = loader._get_csv(
                "https://connect.example.com/export/opportunity/814/user_data/"
            )
            assert rows == []
