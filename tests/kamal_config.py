"""Load Kamal deploy configs the way Kamal itself does: base file, then overlay.

``kamal deploy -c config/deploy-worker.yml -d staging`` deep-merges
``config/deploy-worker.staging.yml`` over ``config/deploy-worker.yml``
(``Kamal::Configuration.load_config_files``). Hashes merge key by key; every
other value — arrays included — is replaced wholesale. Tests assert on the
merged result so a staging assertion covers what actually ships to the host.
"""

import re
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# The one ERB construct in the base configs: a production-only block, written as
# YAML comments so the raw file still parses. See config/deploy.yml.
_PRODUCTION_ONLY = re.compile(
    r'^#<% unless ENV\["KAMAL_DESTINATION"\] %>\n(.*?)^#<% end %>\n',
    re.MULTILINE | re.DOTALL,
)


def _render(text: str, destination: str | None) -> str:
    return _PRODUCTION_ONLY.sub("" if destination else lambda m: m.group(1), text)


def _deep_merge(base: dict, overlay: dict) -> dict:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        merged[key] = (
            _deep_merge(current, value)
            if isinstance(current, dict) and isinstance(value, dict)
            else value
        )
    return merged


def load_config(name: str, destination: str | None = None) -> dict:
    """Return the effective config for ``name``, as Kamal would resolve it."""
    config = yaml.safe_load(_render((CONFIG_DIR / name).read_text(), destination))
    if destination:
        overlay = CONFIG_DIR / f"{Path(name).stem}.{destination}.yml"
        # An overlay stripped down to comments parses as None, not {}.
        config = _deep_merge(config, yaml.safe_load(overlay.read_text()) or {})
    return config


def base_config_names() -> list[str]:
    """Every production Kamal config, excluding the ``.<destination>.yml`` overlays."""
    return sorted(p.name for p in CONFIG_DIR.glob("deploy*.yml") if p.stem.count(".") == 0)
