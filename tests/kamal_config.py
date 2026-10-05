"""Load the Kamal deploy configs the fitness tests assert on."""

from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load_config(name: str) -> dict:
    """Return the parsed config for ``name``. ERB tags only appear inside quoted
    values, so the raw file parses as plain YAML."""
    return yaml.safe_load((CONFIG_DIR / name).read_text())


def base_config_names() -> list[str]:
    """Every Kamal config in ``config/``."""
    return sorted(p.name for p in CONFIG_DIR.glob("deploy*.yml"))
