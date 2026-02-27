import pytest
import requests_mock as rm

from mcp_server.loaders.connect_metadata import ConnectMetadataLoader

BASE = "https://connect.example.com"


@pytest.fixture
def loader():
    return ConnectMetadataLoader(
        opportunity_id=814,
        credential={"type": "oauth", "value": "test-token"},
        base_url=BASE,
    )


class TestConnectMetadataLoader:
    def test_load_returns_metadata(self, loader):
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opp_org_program_list/",
                json={
                    "organizations": [{"id": 1, "slug": "dimagi", "name": "Dimagi"}],
                    "opportunities": [
                        {
                            "id": 814,
                            "name": "CHC Nutrition",
                            "organization": "dimagi",
                            "is_active": True,
                            "program": 25,
                        }
                    ],
                    "programs": [
                        {"id": 25, "name": "CHC Nutrition", "organization": "dimagi"}
                    ],
                },
            )
            m.get(
                f"{BASE}/export/opportunity/814/",
                json={
                    "id": 814,
                    "name": "CHC Nutrition",
                    "description": "A nutrition program",
                    "organization": "dimagi",
                    "is_active": True,
                },
            )

            metadata = loader.load()
            assert metadata["opportunity"]["id"] == 814
            assert metadata["opportunity"]["name"] == "CHC Nutrition"
            assert len(metadata["organizations"]) == 1
            assert len(metadata["programs"]) == 1
            assert metadata["organizations"][0]["slug"] == "dimagi"
