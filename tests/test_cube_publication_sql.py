"""Database-free contract for publication-aware SQL cache/queue identity."""

import pytest

from apps.semantic.services.cube import _publication_scoped_sql


@pytest.mark.parametrize(
    "source_sql",
    [
        'SELECT * FROM "visits"',
        "SELECT category, visited_at FROM visits WHERE category <> 'excluded'",
        "WITH rows AS (SELECT * FROM visits) SELECT * FROM rows",
        "SELECT * FROM visits -- preserve the source's trailing comment",
    ],
)
def test_publication_sql_preserves_source_and_binds_revision_as_a_noop(source_sql):
    sql = _publication_scoped_sql(source_sql)

    assert sql == (
        f"SELECT * FROM (\n{source_sql}\n) AS scout_source\n"
        "WHERE ARRAY[{SECURITY_CONTEXT.cubeDataRevision}]::text[] IS NOT NULL"
    )
    assert sql == _publication_scoped_sql(source_sql)
    assert ".unsafeValue" not in sql
