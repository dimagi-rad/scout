from unittest import mock

import pytest
import requests_mock as rm

from mcp_server.loaders.connect_metadata import ConnectMetadataLoader

BASE = "https://connect.example.com"

# Real Connect /app_structure/ shape: {"learn_app": <HQ app JSON>, "deliver_app": <HQ app JSON>}
# Each app is HQ application JSON: app -> modules -> forms -> questions.
APP_STRUCTURE_PAYLOAD = {
    "learn_app": None,
    "deliver_app": {
        "id": "app_deliver",
        "name": "MUAC Deliver",
        "modules": [
            {
                "name": "Delivery",
                "case_type": "beneficiary",
                "forms": [
                    {
                        "xmlns": "http://openrosa.org/formdesigner/muac1",
                        "name": "MUAC Visit",
                        "questions": [
                            {
                                "label": "MUAC (cm)",
                                "value": "/data/muac_group/muac",
                                "tag": "input",
                                "type": "Decimal",
                                "repeat": False,
                            },
                            {
                                "label": "MUAC confirmed",
                                "value": "/data/muac_group/muac_confirmed",
                                "tag": "select1",
                                "type": "Select",
                                "repeat": False,
                            },
                        ],
                    }
                ],
            }
        ],
    },
}


@pytest.fixture
def loader():
    return ConnectMetadataLoader(
        opportunity_id=814,
        credential={"type": "oauth", "value": "test-token"},
        base_url=BASE,
    )


def _loader():
    return ConnectMetadataLoader(
        opportunity_id=1237,
        credential={"type": "api_key", "value": "tok"},
        base_url="https://connect.example",
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
                    "programs": [{"id": 25, "name": "CHC Nutrition", "organization": "dimagi"}],
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
            m.get(
                f"{BASE}/export/opportunity/814/app_structure/",
                json={"learn_app": None, "deliver_app": None},
            )

            metadata = loader.load()
            assert metadata["opportunity"]["id"] == 814
            assert metadata["opportunity"]["name"] == "CHC Nutrition"
            assert len(metadata["organizations"]) == 1
            assert len(metadata["programs"]) == 1
            assert metadata["organizations"][0]["slug"] == "dimagi"


def test_load_includes_form_definitions_from_app_structure():
    loader = _loader()
    with (
        mock.patch.object(
            loader,
            "_fetch_org_data",
            return_value={"organizations": [], "programs": [], "opportunities": []},
        ),
        mock.patch.object(
            loader, "_fetch_opportunity_detail", return_value={"name": "Demo", "id": 1237}
        ),
        mock.patch.object(loader, "_fetch_app_structure", return_value=APP_STRUCTURE_PAYLOAD),
    ):
        result = loader.load()

    # _extract_form_definitions keys by xmlns (CommCare shape):
    fd = result["form_definitions"]
    assert "http://openrosa.org/formdesigner/muac1" in fd
    form = fd["http://openrosa.org/formdesigner/muac1"]
    q = {item["value"]: item for item in form["questions"]}
    assert q["/data/muac_group/muac"]["type"] == "Decimal"
    assert q["/data/muac_group/muac_confirmed"]["label"] == "MUAC confirmed"
