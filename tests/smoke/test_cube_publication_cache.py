"""DB-free regression against the real Cube compiler, result cache, and queue.

Requires SCOUT_CUBE_COMPILER_CONTAINER naming an existing local Cube container.
Only a separate Node process with an in-memory fake driver runs in the container;
the serving process, its caches, and all databases are untouched.
"""

import json
import os
import shutil
import subprocess

import pytest

from apps.semantic.services.cube import _publication_scoped_sql

_VERIFY = r"""
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { prepareCompiler, PostgresQuery } = require('@cubejs-backend/schema-compiler');
const { QueryCache } = require('@cubejs-backend/query-orchestrator');
const { NativeInstance } = require('@cubejs-backend/native');

async function bounded(promise) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('Synthetic query did not complete')), 3000);
    })]);
  } finally { clearTimeout(timer); }
}

async function run() {
  const source = JSON.parse(readFileSync(0, 'utf8'));
  const schema = { cubes: [{
    name: 'publication_probe', sql: source.sql,
    dimensions: [
      { name: 'id', type: 'number', sql: '{CUBE}.id', primary_key: true },
      { name: 'category', type: 'string', sql: '{CUBE}.category' },
      { name: 'visited_at', type: 'time', sql: '{CUBE}.visited_at' },
    ],
    measures: [{ name: 'count', type: 'count' }],
  }] };
  if (source.joinSql) {
    schema.cubes[0].joins = [{
      name: 'publication_dimensions', relationship: 'many_to_one',
      sql: '{CUBE}.id = {publication_dimensions}.id',
    }];
    schema.cubes.push({
      name: 'publication_dimensions', sql: source.joinSql,
      dimensions: [
        { name: 'id', type: 'number', sql: '{CUBE}.id', primary_key: true },
        { name: 'category', type: 'string', sql: '{CUBE}.category' },
      ],
    });
  }
  const compiler = prepareCompiler({ dataSchemaFiles: async () => [
    { fileName: 'schema.yaml', content: JSON.stringify(schema) },
  ] }, { nativeInstance: new NativeInstance(), omitErrors: true });
  await compiler.compiler.compile();
  assert.deepEqual(compiler.compiler.errorsReporter.getErrors(), []);
  const firstRevision = '2026-09-15T05:33:25.291945Z';
  const secondRevision = '2026-09-15T05:33:25.291946Z';
  const pendingRevision = '2026-09-15T05:33:25.291947Z';
  const repairedRevision = '2026-09-15T05:33:25.291948Z';
  function compile(cubeDataRevision) {
    const query = new PostgresQuery(compiler, {
      dimensions: ['publication_probe.category',
        ...(source.joinSql ? ['publication_dimensions.category'] : [])],
      measures: ['publication_probe.count'],
      filters: [{ member: 'publication_probe.visited_at', operator: 'inDateRange',
        values: ['2026-07-17', '2026-09-15'] }],
      timezone: 'UTC', contextSymbols: { securityContext: { cubeDataRevision } },
    });
    const [sql, values] = query.buildSqlAndParams();
    return { query: sql, values, dataSource: 'default', cacheKeyQueries: [['SELECT 1', []]] };
  }
  const first = compile(firstRevision);
  const second = compile(secondRevision);
  assert(first.values.includes(firstRevision));
  assert(second.values.includes(secondRevision));
  if (source.joinSql) {
    assert(first.query.includes('LEFT JOIN'));
    assert.equal(first.query.match(/::text\[\] IS NOT NULL/g).length, 2);
  }
  assert.equal(first.query, second.query);
  assert.notDeepEqual(QueryCache.queryCacheKey(first), QueryCache.queryCacheKey(second));
  assert.deepEqual(QueryCache.queryCacheKey(second), QueryCache.queryCacheKey(compile(secondRevision)));
  const legacy = compile(undefined);
  assert(legacy.query.includes('ARRAY[]::text[] IS NOT NULL'));
  assert.notDeepEqual(QueryCache.queryCacheKey(legacy), QueryCache.queryCacheKey(first));
  const unsafe = "x'); DROP TABLE visits; --";
  const malicious = compile(unsafe);
  assert(!malicious.query.includes(unsafe));
  assert(malicious.values.includes(unsafe));

  let sourceCount = 4;
  let started;
  let release;
  const pendingStarted = new Promise(resolve => { started = resolve; });
  const pendingRelease = new Promise(resolve => { release = resolve; });
  const driver = { query: async (sql, values) => {
    if (sql === 'SELECT 1') return [{ refresh_key: 1 }];
    const result = [{ count: sourceCount }];
    if (values.includes(pendingRevision)) {
      started();
      await pendingRelease;
    }
    return result;
  } };
  const cache = new QueryCache('publication-probe', async () => driver, () => {}, {
    cacheAndQueueDriver: 'memory',
    queueOptions: async () => ({ concurrency: 2, continueWaitTimeout: 2 }),
  });
  const execute = body => cache.cachedQueryResult(body, []);
  try {
    assert.equal((await execute(first)).data[0].count, 4);
    sourceCount = 5;
    assert.equal((await execute(first)).data[0].count, 4);
    assert.equal((await execute(second)).data[0].count, 5);
    sourceCount = 99;
    assert.equal((await execute(second)).data[0].count, 5);

    sourceCount = 6;
    const pending = execute(compile(pendingRevision));
    try {
      await bounded(pendingStarted);
      sourceCount = 7;
      assert.equal((await bounded(execute(compile(repairedRevision)))).data[0].count, 7);
    } finally { release(); }
    assert.equal((await pending).data[0].count, 6);
    assert.equal((await execute(compile(repairedRevision))).data[0].count, 7);
  } finally {
    release();
    await cache.cleanup();
  }
  process.stdout.write(JSON.stringify({ fresh: 5, pending: 6, repaired: 7 }));
}
run().then(() => process.exit(0)).catch(error => { console.error(error); process.exit(1); });
"""


@pytest.mark.smoke
@pytest.mark.parametrize(
    ("source_sql", "join_sql"),
    [
        ('SELECT * FROM "visits"', None),
        (
            "SELECT category, visited_at FROM visits WHERE category <> 'excluded' -- source comment",
            None,
        ),
        ('SELECT * FROM "visits"', "SELECT id, category FROM categories"),
    ],
    ids=["physical", "custom", "joined-physical-custom"],
)
def test_publication_fences_real_cube_result_cache_and_queued_sql(source_sql, join_sql):
    container = os.environ.get("SCOUT_CUBE_COMPILER_CONTAINER")
    if not container:
        pytest.skip("Set SCOUT_CUBE_COMPILER_CONTAINER to a running local Cube container.")
    docker = shutil.which("docker")
    assert docker, "The real Cube compiler/cache smoke test requires Docker."
    result = subprocess.run(  # noqa: S603
        [docker, "exec", "-i", container, "node", "-e", _VERIFY],
        input=json.dumps(
            {
                "sql": _publication_scoped_sql(source_sql),
                "joinSql": _publication_scoped_sql(join_sql) if join_sql else None,
            }
        ),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"fresh": 5, "pending": 6, "repaired": 7}
