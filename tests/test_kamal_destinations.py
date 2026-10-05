"""Guards that production is the only Kamal deployment (#808).

The retired staging stack deployed with ``kamal deploy -d staging``, deep-merging
``config/<name>.staging.yml`` overlays over the production configs on the same
host. Production must not regain a destination overlay, and every service must
keep its CloudWatch logging and the production network unconditionally.
"""

import pytest

from tests.kamal_config import CONFIG_DIR, base_config_names, load_config

SERVICES = base_config_names()


def test_no_destination_overlays_remain():
    assert [p.name for p in CONFIG_DIR.glob("deploy*.*.yml")] == []
    assert not (CONFIG_DIR / "staging.env").exists()


def test_every_service_config_is_present():
    assert SERVICES == [
        "deploy-cube.yml",
        "deploy-frontend.yml",
        "deploy-mcp.yml",
        "deploy-worker.yml",
        "deploy.yml",
    ]


@pytest.mark.parametrize("name", SERVICES)
def test_every_service_ships_logs_to_cloudwatch_unconditionally(name):
    """The awslogs block used to be ERB-gated on KAMAL_DESTINATION for staging."""
    assert "KAMAL_DESTINATION" not in (CONFIG_DIR / name).read_text()
    assert load_config(name)["logging"]["driver"] == "awslogs"


@pytest.mark.parametrize("name", SERVICES)
def test_every_service_joins_the_production_network(name):
    assert load_config(name)["servers"]["web"]["options"]["network"] == "scout_shared"
