"""Write ephemeral dbt project directories from TransformationAsset records.

Each stage of the transformation pipeline gets a temporary dbt project
containing one .sql file per asset plus a merged schema.yml for tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from apps.transformations.models import TransformationAsset


def write_dbt_project(
    output_dir: Path,
    project_name: str,
    assets: list[TransformationAsset],
) -> Path:
    """Write a complete dbt project directory from TransformationAsset records.

    Creates:
    - output_dir/dbt_project.yml
    - output_dir/models/{asset.name}.sql  (one per asset)
    - output_dir/models/schema.yml  (merged test YAML from assets that have test_yaml)

    Returns output_dir for convenience.
    """
    output_dir = Path(output_dir)
    models_dir = output_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    # dbt_project.yml
    project_config = {
        "name": project_name,
        "version": "1.0.0",
        "config-version": 2,
        "profile": "data_explorer",
        "model-paths": ["models"],
        "test-paths": ["tests"],
        "models": {"+materialized": "table"},
    }
    (output_dir / "dbt_project.yml").write_text(yaml.dump(project_config, default_flow_style=False))

    # Model SQL files
    for asset in assets:
        (models_dir / f"{asset.name}.sql").write_text(asset.sql_content)

    # Merge test YAML fragments into a single schema.yml
    merged_models = []
    for asset in assets:
        if not asset.test_yaml:
            continue
        fragment = yaml.safe_load(asset.test_yaml)
        if fragment and "models" in fragment:
            merged_models.extend(fragment["models"])

    if merged_models:
        schema = {"version": 2, "models": merged_models}
        (models_dir / "schema.yml").write_text(yaml.dump(schema, default_flow_style=False))

    return output_dir
