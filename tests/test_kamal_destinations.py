"""Guards for the staging destination overlays (``kamal deploy -d staging``).

Staging is a deep-merge of ``config/<name>.staging.yml`` over ``config/<name>.yml``,
co-located on the production EC2 box. Two things about that merge are easy to
break silently, and both took production down before: the network aliases that
keep the stacks apart, and the CloudWatch logging block that must not leak into
staging (Docker rejects awslogs-* options on the json-file driver).
"""

import pytest

from tests.kamal_config import CONFIG_DIR, base_config_names, load_config

# Globbed rather than listed: a service added without a staging overlay must
# fail here, not deploy production config under staging's container names.
SERVICES = base_config_names()


@pytest.mark.parametrize("name", SERVICES)
def test_every_service_has_a_staging_overlay(name):
    """Kamal derives the overlay path from the base name; a mismatch silently
    deploys production config under staging's container names."""
    assert (CONFIG_DIR / name.replace(".yml", ".staging.yml")).exists()


@pytest.mark.parametrize("name", SERVICES)
def test_staging_does_not_inherit_production_cloudwatch_logging(name):
    """Production's awslogs block is ERB-gated on KAMAL_DESTINATION rather than
    overridden, because a destination `driver:` would still inherit awslogs-*
    options that Docker rejects on json-file."""
    assert load_config(name, "staging").get("logging") is None
    assert load_config(name)["logging"]["driver"] == "awslogs"


@pytest.mark.parametrize("name", SERVICES)
def test_staging_is_network_isolated_from_production(name):
    """A shared network alias round-robins scout.dimagi.com traffic into staging,
    which then 400s on ALLOWED_HOSTS."""
    production = load_config(name)["servers"]["web"]["options"]
    staging = load_config(name, "staging")["servers"]["web"]["options"]

    assert production["network"] == "scout_shared"
    assert staging["network"] == "scout_staging_shared"
    if "network-alias" in production:
        assert staging["network-alias"] != production["network-alias"]
