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

_RETENTION_VERIFY = r"""
const assert = require('node:assert/strict');
const { QueryCache } = require('@cubejs-backend/query-orchestrator');

async function until(predicate, label) {
  const deadline = Date.now() + 2500;
  while (!predicate()) {
    assert(Date.now() < deadline, label);
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

async function run() {
  let rows = 4;
  let sourceCalls = 0;
  const source = { query: async (sql, values) => {
    if (sql === 'SELECT 1') return [{ refresh_key: 1 }];
    sourceCalls += 1;
    if (values[0] === 'oversized') return [{ payload: 'x'.repeat(65536) }];
    if (values[0].startsWith('byte-pressure-')) return [{ payload: 'x'.repeat(8000) }];
    return [{ count: rows }];
  } };
  const cache = new QueryCache('retention-probe', async () => source, () => {}, {
    cacheAndQueueDriver: 'memory',
    queueOptions: async () => ({ concurrency: 2, continueWaitTimeout: 1 }),
  });
  const driver = cache.getCacheDriver();
  const body = revision => ({
    query: 'SELECT count(*) FROM synthetic WHERE $1 IS NOT NULL',
    values: [revision], dataSource: 'default', cacheKeyQueries: [['SELECT 1', []]],
    cacheMode: 'must-revalidate',
  });
  const execute = revision => cache.cachedQueryResult(body(revision), []);
  let release;
  const held = new Promise(resolve => { release = resolve; });
  let lockStarted = false;
  const lock = driver.withLock('lock:retention-probe', async () => {
    lockStarted = true;
    await held;
  }, 60, true);
  await until(() => lockStarted, 'Lock did not start');
  const marker = 'retention-probe#SQL_PRE_AGGREGATIONS_TABLES_USED:protected';
  await driver.set(marker, { table: 'do_not_drop' }, 60);
  try {
    assert.equal((await execute('publication-0')).data[0].count, 4);
    rows = 5;
    assert.equal((await execute('publication-0')).data[0].count, 4);
    assert.equal((await execute('publication-1')).data[0].count, 5);
    for (let revision = 2; revision < 100; revision += 1) {
      rows = revision;
      assert.equal((await execute(`publication-${revision}`)).data[0].count, revision);
      assert((await driver.keysStartingWith('retention-probe#SQL_QUERY_RESULT:')).length <= 16,
        'Published result entries exceeded the configured bound');
    }
    assert.equal(typeof driver.getStats, 'function', 'Cube image must contain the pinned memory-cache patch');
    assert(driver.getStats().resultBytes <= 32768);
    assert.equal(await driver.withLock('lock:retention-probe', async () => {
      assert.fail('Result pressure evicted an active lock');
    }), false);
    assert.deepEqual(await driver.get(marker), { table: 'do_not_drop' });
    const callsBeforeHit = sourceCalls;
    assert.equal((await execute('publication-99')).data[0].count, 99);
    assert.equal(sourceCalls, callsBeforeHit, 'Newest publication should remain cached');
    rows = 101;
    assert.equal((await execute('publication-0')).data[0].count, 101, 'Evicted entries must requery');

    // Each result fits individually, but together they must trigger byte
    // eviction before reaching the separate 16-entry cap.
    for (let revision = 0; revision < 6; revision += 1) {
      assert.equal((await execute(`byte-pressure-${revision}`)).data[0].payload.length, 8000);
      assert(driver.getStats().resultBytes <= 32768);
    }
    assert(driver.getStats().resultEntries < 16);
    assert.equal(await cache.resultFromCacheIfExists(body('byte-pressure-0')), null);
    const callsBeforeByteHit = sourceCalls;
    assert.equal((await execute('byte-pressure-5')).data[0].payload.length, 8000);
    assert.equal(sourceCalls, callsBeforeByteHit);

    // Keep only a small bounded terminal error, never oversized rows. Both
    // the first caller and later polls receive a useful error without a rerun.
    const tooLarge = /Query result exceeds the configured in-memory cache limit/;
    await assert.rejects(execute('oversized'), tooLarge);
    const callsAfterOversized = sourceCalls;
    await assert.rejects(execute('oversized'), tooLarge);
    assert.equal(sourceCalls, callsAfterOversized);
    await assert.rejects(cache.resultFromCacheIfExists(body('oversized')), tooLarge);
    assert(driver.getStats().resultBytes <= 32768);
    assert.deepEqual(await driver.get(marker), { table: 'do_not_drop' });
    assert.equal(await driver.withLock('lock:retention-probe', async () => {
      assert.fail('Byte pressure evicted an active lock');
    }), false);

    release();
    assert.equal(await lock, true);
    driver.reset();
    assert.equal(driver.getStats().resultEntries, 0);
    assert.equal(driver.getStats().timerActive, false);
    await driver.set('retention-probe#SQL_QUERY_RESULT:idle', { count: 7 }, 0.03);
    assert.equal(driver.getStats().resultEntries, 1);
    assert.equal(driver.getStats().timerActive, true);
    // getStats must not perform expiry. This proves the one background timer
    // reclaims an expired key that nobody reads again, not just lazy get().
    await until(() => driver.getStats().resultEntries === 0, 'Idle result did not expire');
    assert.equal(driver.getStats().resultBytes, 0);
    assert.equal(driver.getStats().timerActive, false);
    process.stdout.write(JSON.stringify({ publications: 100, maxEntries: 16, maxBytes: 32768,
      idleExpiry: true, protectedLock: true, protectedMetadata: true,
      byteEviction: true, oversizedRejected: true }));
  } finally {
    release();
    await lock;
    driver.reset();
    await cache.cleanup();
  }
}
run().then(() => process.exit(0)).catch(error => { console.error(error); process.exit(1); });
"""

_POLL_VERIFY = r"""
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { realpathSync } = require('node:fs');
const serverRequire = createRequire(realpathSync('/cube/node_modules/.bin/cubejs-server'));
const { OrchestratorApi } = serverRequire('@cubejs-backend/server-core/dist/src/core/OrchestratorApi');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const outcome = promise => promise.then(value => ({ value }), error => ({ error }));

async function verify(oversized) {
  let sourceCalls = 0;
  let releaseSource;
  const held = new Promise(resolve => { releaseSource = resolve; });
  const source = { query: async sql => {
    if (sql === 'SELECT 1') return [{ refresh_key: 1 }];
    sourceCalls += 1;
    await held;
    return oversized ? [{ payload: 'x'.repeat(4096) }] : [{ count: 7 }];
  } };
  const api = new OrchestratorApi(async () => source, () => {}, {
    redisPrefix: `slow-poll-${oversized ? 'oversized' : 'normal'}`,
    continueWaitTimeout: 0.02,
    cacheAndQueueDriver: 'memory',
    contextToDbType: async () => 'postgres',
    contextToExternalDbType: () => 'postgres',
    queryCacheOptions: { queueOptions: async () => ({
      concurrency: 2, continueWaitTimeout: 0.06, executionTimeout: 3, orphanedTimeout: 1,
    }) },
  });
  const driver = api.getQueryOrchestrator().getQueryCache().getCacheDriver();
  const body = {
    query: 'SELECT payload FROM synthetic', values: ['one-publication'],
    dataSource: 'default', cacheKeyQueries: [['SELECT 1', []]],
    cacheMode: 'must-revalidate', requestId: 'slow-poll-regression',
  };
  try {
    const first = await outcome(api.executeQuery(body));
    assert.equal(first.error?.error, 'Continue wait');
    assert.equal(sourceCalls, 1);
    releaseSource();
    // The timed-out first caller must finish its own cache/queue handoff
    // before we poll, reproducing the retry that otherwise reruns forever.
    await sleep(100);
    let final;
    for (let poll = 0; poll < 10; poll += 1) {
      final = await outcome(api.executeQuery(body));
      assert.equal(sourceCalls, 1, 'Polling must not rerun the completed source query');
      if (final.error?.error !== 'Continue wait') break;
      await sleep(20);
    }
    if (oversized) {
      assert.match(final.error?.error || '',
        /Query result exceeds the configured in-memory cache limit/);
      const retry = await outcome(api.executeQuery(body));
      assert.match(retry.error?.error || '', /Reduce the number of rows or columns/);
      assert.equal(sourceCalls, 1);
      assert(driver.getStats().resultBytes <= 1024);
    } else {
      assert.equal(final.error, undefined);
      assert.equal(final.value.data[0].count, 7);
    }
  } finally {
    releaseSource();
    driver.reset();
    await api.release();
  }
}

async function run() {
  await verify(false);
  await verify(true);
  process.stdout.write(JSON.stringify({ normalPollCompleted: true, oversizedPollTerminated: true }));
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


@pytest.mark.smoke
def test_real_cube_retention_is_bounded_and_expires_without_rereading():
    container = os.environ.get("SCOUT_CUBE_COMPILER_CONTAINER")
    if not container:
        pytest.skip("Set SCOUT_CUBE_COMPILER_CONTAINER to a patched local Cube container.")
    docker = shutil.which("docker")
    assert docker, "The real Cube retention smoke test requires Docker."
    result = subprocess.run(  # noqa: S603
        [
            docker,
            "exec",
            "-e",
            "SCOUT_CUBE_CACHE_MAX_RESULT_ENTRIES=16",
            "-e",
            "SCOUT_CUBE_CACHE_MAX_RESULT_BYTES=32768",
            "-e",
            "SCOUT_CUBE_CACHE_SWEEP_INTERVAL_MS=20",
            container,
            "node",
            "-e",
            _RETENTION_VERIFY,
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "publications": 100,
        "maxEntries": 16,
        "maxBytes": 32768,
        "idleExpiry": True,
        "protectedLock": True,
        "protectedMetadata": True,
        "byteEviction": True,
        "oversizedRejected": True,
    }


@pytest.mark.smoke
def test_real_cube_slow_query_polls_complete_or_return_terminal_size_error():
    container = os.environ.get("SCOUT_CUBE_COMPILER_CONTAINER")
    if not container:
        pytest.skip("Set SCOUT_CUBE_COMPILER_CONTAINER to a patched local Cube container.")
    docker = shutil.which("docker")
    assert docker, "The real Cube polling smoke test requires Docker."
    result = subprocess.run(  # noqa: S603
        [
            docker,
            "exec",
            "-e",
            "SCOUT_CUBE_CACHE_MAX_RESULT_BYTES=1024",
            container,
            "node",
            "-e",
            _POLL_VERIFY,
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "normalPollCompleted": True,
        "oversizedPollTerminated": True,
    }
