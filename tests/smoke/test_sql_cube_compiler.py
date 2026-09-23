"""Round-trip literal SQL through the real Cube compiler without querying data.

SCOUT_CUBE_COMPILER_CONTAINER=scout-cube-1 uv run pytest \
    tests/smoke/test_sql_cube_compiler.py -m smoke --override-ini='addopts=' \
    -p no:django --confcutdir=tests/smoke -q
"""

import json
import os
import shutil
import subprocess

import pytest

from apps.semantic.services.cube_sql import embed_cube_sql

_COMPILE = r"""
const {readFileSync} = require('node:fs');
const {prepareCompiler, PostgresQuery} = require('@cubejs-backend/schema-compiler');
const {NativeInstance} = require('@cubejs-backend/native');
(async () => {
  const input = JSON.parse(readFileSync(0, 'utf8'));
  const cubes = input.sources.map((source, i) => ({name: `fixture_${i}`, sql: source,
    dimensions: [{name: 'topic', type: 'string', sql: '{CUBE}."topic"'}],
    measures: [{name: 'count', type: 'count'},
      {name: 'filtered', type: 'count', filters: [{sql: input.filter}]},
      {name: 'ratio', type: 'number', sql: input.ratio}]}));
  const compiler = prepareCompiler({dataSchemaFiles: async () => [
    {fileName: 'schema.yaml', content: JSON.stringify({cubes})}
  ]}, {nativeInstance: new NativeInstance(), omitErrors: true});
  await compiler.compiler.compile();
  const errors = compiler.compiler.errorsReporter.getErrors();
  if (errors.length) throw new Error(errors.map(e => e.message).join('\n'));
  const queries = cubes.map(cube => new PostgresQuery(compiler, {
    dimensions: [`${cube.name}.topic`],
    measures: [`${cube.name}.count`, `${cube.name}.filtered`, `${cube.name}.ratio`], timezone: 'UTC'
  }).buildSqlAndParams()[0]);
  process.stdout.write(JSON.stringify(queries), () => process.exit(0));
})().catch(error => { console.error(error); process.exit(1); });
"""


@pytest.mark.smoke
def test_custom_sql_literals_are_unchanged_by_cube_compilation():
    container = os.environ.get("SCOUT_CUBE_COMPILER_CONTAINER")
    if not container:
        pytest.skip("Set SCOUT_CUBE_COMPILER_CONTAINER to a running local Cube container.")
    docker = shutil.which("docker")
    assert docker, "The real Cube compiler smoke test requires Docker."
    cases = [
        "SELECT 'Account update' AS topic FROM raw_messages",
        "SELECT '{missing_label}' AS topic FROM raw_messages",
        "SELECT form_json #>> '{form,topic}' AS topic FROM raw_visits",
        r"SELECT regexp_replace(content, '\d{2}', '{\1}', 'g') AS topic FROM raw_messages",
        "SELECT '{% invalid_jinja %} {{jinja}}' AS topic FROM raw_cases",
        'SELECT "column{CUBE}" AS topic FROM "table{count}"',
        "SELECT 'line\nbreak\t{CUBE}' AS topic FROM raw_visits -- {unknown}\n",
        r"SELECT '\u007b' AS topic FROM raw_messages",
        "SELECT '` + 1 + ` ${CUBE}' AS topic FROM raw_messages -- `template`\n",
    ]
    result = subprocess.run(  # noqa: S603
        [docker, "exec", "-i", container, "node", "-e", _COMPILE],
        input=json.dumps(
            {
                "sources": [embed_cube_sql(source) for source in cases],
                "filter": embed_cube_sql("""{CUBE}."topic" ~ '[0-9]{2}'""", references={"CUBE"}),
                "ratio": embed_cube_sql(
                    "{CUBE.filtered}::numeric / NULLIF({count}, 0)",
                    references={"CUBE.filtered", "count"},
                ),
            }
        ),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for source, query in zip(cases, json.loads(result.stdout), strict=True):
        assert source in query
        assert "'[0-9]{2}'" in query
        assert "NULLIF(count(*), 0)" in query
