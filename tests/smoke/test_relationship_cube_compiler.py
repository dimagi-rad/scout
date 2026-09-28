"""Verify Cube's fan-out protection using real compilation and PostgreSQL."""

import json
import os
import shutil
import subprocess

import psycopg
import pytest

_COMPILE = r"""
const {prepareCompiler, PostgresQuery} = require('@cubejs-backend/schema-compiler');
const {NativeInstance} = require('@cubejs-backend/native');
(async () => {
  const cubes = [
    {name: 'raw_forms', sql: "SELECT * FROM (VALUES ('f1'), ('f2')) t(form_id)",
      dimensions: [{name: 'form_id', sql: '{CUBE}.form_id', type: 'string', primary_key: true}],
      measures: [{name: 'count', type: 'count'}],
      joins: [{name: 'raw_form_cases', relationship: 'one_to_many',
        sql: '{raw_forms.form_id} = {raw_form_cases.form_id}'}]},
    {name: 'raw_form_cases',
      sql: "SELECT * FROM (VALUES ('f1c1','f1','c1'), ('f1c2','f1','c2'), ('f2c1','f2','c1')) t(form_case_id,form_id,case_id)",
      dimensions: [
        {name: 'form_case_id', sql: '{CUBE}.form_case_id', type: 'string', primary_key: true},
        {name: 'form_id', sql: '{CUBE}.form_id', type: 'string'},
        {name: 'case_id', sql: '{CUBE}.case_id', type: 'string'}],
      joins: [{name: 'raw_cases', relationship: 'many_to_one',
        sql: '{raw_form_cases.case_id} = {raw_cases.case_id}'}]},
    {name: 'raw_cases', sql: "SELECT * FROM (VALUES ('c1','same'), ('c2','same')) t(case_id,case_type)",
      dimensions: [
        {name: 'case_id', sql: '{CUBE}.case_id', type: 'string', primary_key: true},
        {name: 'case_type', sql: '{CUBE}.case_type', type: 'string'}]}
  ];
  const compiler = prepareCompiler({dataSchemaFiles: async () => [
    {fileName: 'schema.yaml', content: JSON.stringify({cubes})}
  ]}, {nativeInstance: new NativeInstance(), omitErrors: true});
  await compiler.compiler.compile();
  const errors = compiler.compiler.errorsReporter.getErrors();
  if (errors.length) throw new Error(errors.map(e => e.message).join('\n'));
  const query = new PostgresQuery(compiler, {measures: ['raw_forms.count'],
    dimensions: ['raw_cases.case_type'], timezone: 'UTC'}).buildSqlAndParams();
  process.stdout.write(JSON.stringify(query), () => process.exit(0));
})().catch(error => { console.error(error); process.exit(1); });
"""


@pytest.mark.smoke
def test_form_count_is_not_multiplied_by_case_associations():
    container = os.environ.get("SCOUT_CUBE_COMPILER_CONTAINER")
    database_url = os.environ.get("SCOUT_SMOKE_DATABASE_URL")
    if not container or not database_url:
        pytest.skip("Set a local Cube container and a disposable PostgreSQL database URL.")
    docker = shutil.which("docker")
    assert docker
    result = subprocess.run(  # noqa: S603
        [docker, "exec", container, "node", "-e", _COMPILE],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    sql, parameters = json.loads(result.stdout)
    assert not parameters
    with psycopg.connect(database_url, options="-c default_transaction_read_only=on") as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            # Three associations, but only two distinct forms in the category.
            assert cursor.fetchall() == [("same", 2)]
