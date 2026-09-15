"""Read-only calculated-dimension regression against the real Cube compiler.

Run with a running local Cube container; no database/provider access is used::

    SCOUT_CUBE_COMPILER_CONTAINER=scout-cube-1 uv run python -m pytest \
      tests/smoke/test_dimension_cube_compiler.py -m smoke --override-ini='addopts=' \
      -p no:django --confcutdir=tests/smoke -q
"""

import json
import os
import shutil
import subprocess

import pytest

from apps.semantic.services.field_sql import compile_dimension_sql

_COMPILE = """
const { readFileSync } = require('node:fs');
const { prepareCompiler, PostgresQuery } = require('@cubejs-backend/schema-compiler');
const { NativeInstance } = require('@cubejs-backend/native');
async function run() {
  const expressions = JSON.parse(readFileSync(0, 'utf8'));
  const schema = { cubes: [{
    name: 'review_fixture', sql_table: 'fixture_rows',
    dimensions: expressions.map((sql, index) => ({ name: `label_${index}`, type: 'string', sql })),
    measures: [{ name: 'count', type: 'count' }],
  }] };
  const compiler = prepareCompiler({ dataSchemaFiles: async () => [
    { fileName: 'schema.yaml', content: JSON.stringify(schema) },
  ] }, { nativeInstance: new NativeInstance(), omitErrors: true });
  await compiler.compiler.compile();
  const errors = compiler.compiler.errorsReporter.getErrors();
  if (errors.length) throw new Error(errors.map(error => error.message).join('\\n'));
  const sql = expressions.map((_, index) => {
    const query = new PostgresQuery(compiler, {
      dimensions: [`review_fixture.label_${index}`], measures: ['review_fixture.count'], timezone: 'UTC',
    });
    query.buildSqlAndParams();
    return query.dimensions[0].dimensionSql();
  });
  process.stdout.write(JSON.stringify(sql));
}
run().then(() => process.exit(0)).catch(error => { console.error(error); process.exit(1); });
"""


@pytest.mark.smoke
def test_calculated_dimension_braces_survive_real_cube_yaml_compiler():
    container = os.environ.get("SCOUT_CUBE_COMPILER_CONTAINER")
    if not container:
        pytest.skip("Set SCOUT_CUBE_COMPILER_CONTAINER to a running local Cube container.")
    docker = shutil.which("docker")
    assert docker, "The real Cube compiler smoke test requires Docker."
    cases = [
        ("'{CUBE}'", "'{CUBE}'"),
        ("'{does_not_exist}'", "'{does_not_exist}'"),
        ('\'{"topic": "Account"}\'', '\'{"topic": "Account"}\''),
        ("'{{already_doubled}}'", "'{{already_doubled}}'"),
        ("'{'", "'{'"),
        ("'}'", "'}'"),
        ('{CUBE}."content" /* {does_not_exist} */', '"review_fixture"."content"'),
        ("content -- {does_not_exist}", '"review_fixture"."content"'),
        ('{CUBE}."column{CUBE}"', '"review_fixture"."column{CUBE}"'),
        ('"column{does_not_exist}"', '"review_fixture"."column{does_not_exist}"'),
        ("concat(content, '{CUBE}')", 'pg_catalog.CONCAT("review_fixture"."content", \'{CUBE}\')'),
        (r"'\u007b'", r"'\u007b'"),
        ("'line\nbreak {CUBE}'", "'line\nbreak {CUBE}'"),
        ("'{% invalid_jinja %}'", "'{% invalid_jinja %}'"),
        (
            r"regexp_replace(content, '\d{2}', '{\1}', 'g')",
            r"""pg_catalog.REGEXP_REPLACE("review_fixture"."content", '\d{2}', '{\1}', 'g')""",
        ),
    ]
    expressions = [
        compile_dimension_sql(source, columns={"content", "column{CUBE}", "column{does_not_exist}"})
        for source, _expected in cases
    ]
    # The container name is a single argument; only the fixed, reviewed script runs (no shell).
    result = subprocess.run(  # noqa: S603
        [docker, "exec", "-i", container, "node", "-e", _COMPILE],
        input=json.dumps(expressions),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [expected for _source, expected in cases]
