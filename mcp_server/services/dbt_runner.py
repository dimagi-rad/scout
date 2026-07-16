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
from urllib.parse import unquote, urlparse

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
    confinement_role: str | None = None,
) -> None:
    """Generate a dbt profiles.yml targeting the tenant's schema.

    Two confinement controls are written into the profile (issue #241):

    - ``search_path`` is pinned to ``schema_name`` ONLY (not ``$user,public``).
      Postgres' default search path would otherwise resolve the SELECT in a
      generated staging model (e.g. ``FROM raw_cases``) against ``public`` even
      though dbt creates the relation in the tenant schema, so every model would
      fail silently (04#4). Restricting the path to the single schema also means
      an unqualified table reference can never reach another tenant's schema.
    - ``role`` (when ``confinement_role`` is given) makes dbt ``SET ROLE`` to a
      dedicated low-privilege, NOLOGIN role on every connection, so user-authored
      SQL does NOT execute with the full ``MANAGED_DATABASE_URL`` superuser's
      privileges (04#3 SECURITY). That role holds rights on this schema only, so
      a fully-qualified cross-tenant read (``FROM other_schema.raw_cases``) is
      blocked by missing USAGE rather than allowed as superuser.

    Args:
        output_path: Where to write the profiles.yml.
        schema_name: PostgreSQL schema name for this tenant.
        db_url: PostgreSQL connection URL (postgresql://user:pass@host:port/dbname).
        threads: dbt parallelism (default 4).
        confinement_role: Low-privilege role dbt should ``SET ROLE`` to. When
            ``None`` the ``role`` key is omitted (dbt connects as the URL user).
    """
    parsed = urlparse(db_url)
    # urlparse leaves username/password percent-encoded; resolve-database-url.sh
    # URL-encodes the RDS-managed password (which rotates to values with special
    # chars). psycopg/Django decode automatically, but dbt receives whatever we
    # write here verbatim, so decode to match — otherwise auth fails whenever the
    # password contains an encoded char (SCOUT-DJANGO-1T).
    output = {
        "type": "postgres",
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "user": unquote(parsed.username) if parsed.username else "",
        "password": unquote(parsed.password) if parsed.password else "",
        "dbname": parsed.path.lstrip("/") if parsed.path else "",
        "schema": schema_name,
        # Confine resolution to the target schema only — see docstring.
        "search_path": schema_name,
        "threads": threads,
    }
    if confinement_role:
        output["role"] = confinement_role
    profile = {
        "data_explorer": {
            "target": "tenant_schema",
            "outputs": {"tenant_schema": output},
        }
    }
    Path(output_path).write_text(yaml.dump(profile, default_flow_style=False))
    logger.debug(
        "Generated profiles.yml at %s for schema '%s' (role=%s)",
        output_path,
        schema_name,
        confinement_role or "<url user>",
    )


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
        # A node-level model failure sets res.success=False but leaves
        # res.exception None — the real cause (e.g. 'column "x" does not exist')
        # lives in each node's message. Surface those instead of the opaque
        # "dbt run failed" so callers/Sentry don't have to dig through dbt logs.
        node_errors = [
            f"{r.node.name}: {r.message}"
            for r in (res.result or [])
            if hasattr(r, "node")
            and str(getattr(r, "status", "")) in ("error", "fail")
            and getattr(r, "message", None)
        ]
        if res.exception:
            error_msg = str(res.exception)
        elif node_errors:
            error_msg = "; ".join(node_errors)
        else:
            error_msg = "dbt run failed"
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


def run_dbt_test(
    dbt_project_dir: str,
    profiles_dir: str,
    models: list[str] | None = None,
) -> dict:
    """Run dbt tests via the programmatic Python API.

    Args:
        dbt_project_dir: Directory containing dbt_project.yml.
        profiles_dir: Directory containing the generated profiles.yml.
        models: Optional list of model names to scope tests to.

    Returns:
        {"success": bool, "tests": {model_name: [{"test": str, "status": str, "message": str}]}, "error": str | None}

    Test results are grouped by the model name they test (extracted from
    ``node.attached_node``), so the caller can look up results by model name.
    """
    cli_args = ["test", "--project-dir", dbt_project_dir, "--profiles-dir", profiles_dir]
    if models:
        cli_args.extend(["--select", " ".join(models)])

    logger.info("Invoking dbt test: %s", " ".join(cli_args))

    with _dbt_lock:
        dbt = dbtRunner()
        res = dbt.invoke(cli_args)

    # Always parse results — dbt sets success=False on test failures but still
    # populates res.result with per-test RunResult objects.
    # Group test results by the model they test.
    # Schema test nodes expose ``attached_node`` = "model.<project>.<model_name>".
    test_results: dict[str, list[dict]] = {}
    for r in res.result or []:
        if not (hasattr(r, "node") and hasattr(r, "status")):
            continue
        attached = getattr(r.node, "attached_node", None) or ""
        model_name = attached.split(".")[-1] if attached.startswith("model.") else None
        entry = {
            "test": r.node.name,
            "status": str(r.status),
            "message": getattr(r, "message", ""),
        }
        if model_name:
            test_results.setdefault(model_name, []).append(entry)

    if not res.success:
        error_msg = str(res.exception) if res.exception else "dbt test failed"
        logger.error("dbt test failed: %s", error_msg)
        return {"success": False, "tests": test_results, "error": error_msg}

    logger.info(
        "dbt test complete: %d tests across %d models",
        sum(len(v) for v in test_results.values()),
        len(test_results),
    )
    return {"success": True, "tests": test_results, "error": None}
