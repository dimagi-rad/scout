"""YAML-based pipeline registry for materialization pipelines."""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_PIPELINES_DIR = pathlib.Path(__file__).parent.parent / "pipelines"


@dataclass
class SourceConfig:
    name: str
    description: str = ""


@dataclass
class MetadataDiscoveryConfig:
    description: str = ""


@dataclass
class TransformConfig:
    dbt_project: str
    models: list[str] = field(default_factory=list)


@dataclass
class PipelineConfig:
    name: str
    description: str
    version: str
    provider: str
    sources: list[SourceConfig] = field(default_factory=list)
    metadata_discovery: MetadataDiscoveryConfig | None = None
    transforms: TransformConfig | None = None

    @property
    def has_metadata_discovery(self) -> bool:
        return self.metadata_discovery is not None

    @property
    def dbt_models(self) -> list[str]:
        return self.transforms.models if self.transforms else []


class PipelineRegistry:
    """Loads and caches pipeline definitions from YAML files."""

    def __init__(self, pipelines_dir: str | None = None) -> None:
        self._dir = pathlib.Path(pipelines_dir) if pipelines_dir else _DEFAULT_PIPELINES_DIR
        self._cache: dict[str, PipelineConfig] | None = None

    def _load_all(self) -> dict[str, PipelineConfig]:
        if self._cache is not None:
            return self._cache
        configs: dict[str, PipelineConfig] = {}
        for path in self._dir.glob("*.yml"):
            try:
                with path.open() as f:
                    data = yaml.safe_load(f)
                config = _parse_pipeline(data)
                configs[config.name] = config
            except Exception:
                logger.exception("Failed to load pipeline from %s", path)
        self._cache = configs
        return configs

    def get(self, name: str) -> PipelineConfig | None:
        return self._load_all().get(name)

    def list(self) -> list[PipelineConfig]:
        return list(self._load_all().values())


def _parse_pipeline(data: dict) -> PipelineConfig:
    sources = [
        SourceConfig(name=s["name"], description=s.get("description", ""))
        for s in data.get("sources", [])
    ]
    md_raw = data.get("metadata_discovery")
    metadata_discovery = (
        MetadataDiscoveryConfig(description=md_raw.get("description", "")) if md_raw else None
    )
    tr_raw = data.get("transforms")
    transforms = (
        TransformConfig(
            dbt_project=tr_raw["dbt_project"],
            models=tr_raw.get("models", []),
        )
        if tr_raw
        else None
    )
    return PipelineConfig(
        name=data["pipeline"],
        description=data.get("description", ""),
        version=data.get("version", "1.0"),
        provider=data.get("provider", "commcare"),
        sources=sources,
        metadata_discovery=metadata_discovery,
        transforms=transforms,
    )


_registry: PipelineRegistry | None = None


def get_registry() -> PipelineRegistry:
    global _registry
    if _registry is None:
        _registry = PipelineRegistry()
    return _registry
