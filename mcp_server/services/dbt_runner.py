"""DBT runner using the programmatic Python API (dbtRunner).

Avoids subprocess overhead. Generates a runtime profiles.yml targeting the
tenant's schema, then invokes dbt via the Python API.

dbtRunner is NOT thread-safe — concurrent in-process invocations will corrupt
dbt's global state. A module-level lock serialises all calls.

Reference: https://docs.getdbt.com/reference/programmatic-invocations
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from urllib.parse import urlparse

import yaml
from dbt.cli.main import dbtRunner

logger = logging.getLogger(__name__)

# Serialise all dbt invocations — dbtRunner is not thread-safe.
_dbt_lock = threading.Lock()


def generate_profiles_yml(
    output_path: Path,
    schema_name: str,
    db_url: str,
    threads: int = 4,
) -> None:
    """Generate a dbt profiles.yml targeting the tenant's schema.

    Args:
        output_path: Where to write the profiles.yml.
        schema_name: PostgreSQL schema name for this tenant.
        db_url: PostgreSQL connection URL (postgresql://user:pass@host:port/dbname).
        threads: dbt parallelism (default 4).
    """
    parsed = urlparse(db_url)
    profile = {
        "data_explorer": {
            "target": "tenant_schema",
            "outputs": {
                "tenant_schema": {
                    "type": "postgres",
                    "host": parsed.hostname or "localhost",
                    "port": parsed.port or 5432,
                    "user": parsed.username or "",
                    "password": parsed.password or "",
                    "dbname": parsed.path.lstrip("/") if parsed.path else "",
                    "schema": schema_name,
                    "threads": threads,
                }
            },
        }
    }
    Path(output_path).write_text(yaml.dump(profile, default_flow_style=False))
    logger.debug("Generated profiles.yml at %s for schema '%s'", output_path, schema_name)


def run_dbt(
    dbt_project_dir: str,
    profiles_dir: str,
    models: list[str],
) -> dict:
    """Run dbt models via the programmatic Python API.

    Uses ``dbtRunner`` from ``dbt.cli.main`` — no subprocess needed.
    Acquires ``_dbt_lock`` before invoking to prevent concurrent in-process
    calls from corrupting dbt's global state.

    Args:
        dbt_project_dir: Directory containing dbt_project.yml.
        profiles_dir: Directory containing the generated profiles.yml.
        models: List of dbt model names to run.

    Returns:
        {"success": bool, "models": {name: status}, "error": str | None}
    """
    select_arg = " ".join(models)
    cli_args = [
        "run",
        "--project-dir",
        dbt_project_dir,
        "--profiles-dir",
        profiles_dir,
        "--select",
        select_arg,
    ]

    logger.info("Invoking dbt programmatically: %s", " ".join(cli_args))

    with _dbt_lock:
        dbt = dbtRunner()
        res = dbt.invoke(cli_args)

    if not res.success:
        error_msg = str(res.exception) if res.exception else "dbt run failed"
        logger.error("dbt run failed: %s", error_msg)
        return {"success": False, "error": error_msg, "models": {}}

    model_results = {
        r.node.name: str(r.status)
        for r in (res.result or [])
        if hasattr(r, "node") and hasattr(r, "status")
    }

    for model in models:
        if model not in model_results:
            model_results[model] = "unknown"

    logger.info("dbt run complete: %s", model_results)
    return {"success": True, "models": model_results}
