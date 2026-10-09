"""Real dbt build of long sibling models whose dbt backup names used to collide.

dbt-postgres cut a model name to 51 bytes before appending ``__dbt_backup``, so two
63-byte models sharing a long prefix raced onto one backup relation under
``threads > 1`` (SCOUT-DJANGO-32/34). Requires MANAGED_DATABASE_URL, as in
test_dbt_confinement.py; never point it at a development or provider database.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import psycopg.sql
import pytest

from apps.transformations.services.dbt_project import write_dbt_project
from mcp_server.services.dbt_runner import generate_profiles_yml, run_dbt

pytestmark = pytest.mark.skipif(
    not os.environ.get("MANAGED_DATABASE_URL"), reason="MANAGED_DATABASE_URL not set"
)

SIBLING_PREFIX = "stg_form_final_quiz_test_your_knowledge__repeat_join_"
MATERIALIZATIONS = {"aaa": "table", "bbb": "table", "ccc": "view", "ddd": "incremental"}
SIBLINGS = [f"{SIBLING_PREFIX}{tail}".ljust(63, "x")[:63] for tail in MATERIALIZATIONS]
SHORT = "stg_cases"

NAME_SQL = (
    "select '{{ make_backup_relation(this, \"table\").identifier }}'::text as backup_name, "
    "'{{ make_intermediate_relation(this).identifier }}'::text as tmp_name"
)


class _Asset:
    def __init__(self, name, materialized="table"):
        self.name = name
        self.sql_content = f"{{{{ config(materialized='{materialized}') }}}}\n{NAME_SQL}"
        self.test_yaml = ""


@pytest.fixture
def schema():
    name = f"dbtnames_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(os.environ["MANAGED_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(name)))
        try:
            yield name, conn
        finally:
            conn.execute(
                psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(name))
            )


def test_long_sibling_models_get_distinct_backup_names(schema, tmp_path, monkeypatch):
    monkeypatch.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
    schema_name, conn = schema
    assert len({m[:51] for m in SIBLINGS}) == 1
    models = [*SIBLINGS, SHORT]
    assets = [
        *(_Asset(m, kind) for m, kind in zip(SIBLINGS, MATERIALIZATIONS.values(), strict=True)),
        _Asset(SHORT),
    ]
    project = write_dbt_project(tmp_path / "project", "scout_names", assets)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    generate_profiles_yml(
        profiles / "profiles.yml", schema_name, os.environ["MANAGED_DATABASE_URL"]
    )

    # The second run renames each existing relation to its backup, the step that collided.
    for _ in range(2):
        result = run_dbt(str(project), str(profiles), models)
        assert result["success"], result.get("error")

    names = {}
    for model in models:
        row = conn.execute(
            psycopg.sql.SQL("SELECT backup_name, tmp_name FROM {}").format(
                psycopg.sql.Identifier(schema_name, model)
            )
        ).fetchone()
        names[model] = row

    assert names[SHORT] == (f"{SHORT}__dbt_backup", f"{SHORT}__dbt_tmp")
    backups = {names[m][0] for m in SIBLINGS}
    temps = {names[m][1] for m in SIBLINGS}
    assert len(backups) == len(temps) == len(SIBLINGS)
    for backup, tmp in (names[m] for m in SIBLINGS):
        assert len(backup) <= 63
        assert len(tmp) <= 63
        assert backup.endswith("__dbt_backup")
        assert tmp.endswith("__dbt_tmp")

    relations = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (schema_name,),
        ).fetchall()
    }
    assert relations == set(models)
