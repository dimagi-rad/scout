const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

// Exercise the production configuration without a network connection or npm
// install. pg.Pool and Cube's PostgresDriver are stubbed; ID generation, driver
// configuration and the connection limit run for real.
class FakeClient extends EventEmitter {
  end() { this.emit('end'); }
}

class FakePostgresDriver {
  constructor(config) { this.config = config; this.options = config.options; }
  async createConnection() {
    if (this.config.failConnect) throw new Error('Synthetic connect failure');
    return new FakeClient();
  }
}

function loadConfig(query = () => { throw new Error('Unexpected database access'); }, pools = [], env = {}, catalog = { roleExists: true, probes: 0, warnings: [] }, PostgresDriver = FakePostgresDriver) {
  const sandbox = {
    module: { exports: {} },
    process: { env },
    URL,
    console: { warn: (message) => catalog.warnings.push(message) },
    require: (name) => {
      if (name === 'pg') {
        return { Pool: class {
          constructor(options) { this.options = options; pools.push(options); }
          query(text, values) {
            if (text.includes('to_regrole')) {
              catalog.probes += 1;
              assert.deepEqual(Array.from(values), ['scout_cube_catalog']);
              if (catalog.probeError) return Promise.reject(new Error('synthetic probe failure'));
              assert.match(text, /has_table_privilege\(to_regrole\(\$1\)::oid, 'semantic_cubeschema', 'SELECT'\)/);
              return Promise.resolve({ rows: [{ ready: catalog.roleExists }] });
            }
            return query(text, values, this.options);
          }
        } };
      }
      if (name === '@cubejs-backend/postgres-driver') return { PostgresDriver };
      if (name.startsWith('./')) return require(join(__dirname, name));
      return require(name);
    },
  };
  vm.runInNewContext(readFileSync(join(__dirname, 'cube.js'), 'utf8'), sandbox);
  return sandbox.module.exports;
}
const config = loadConfig();
const base = {
  workspaceId: '11111111-1111-4111-8111-111111111111',
  semanticModelId: '22222222-2222-4222-8222-222222222222',
  cubeSchemaHash: 'a'.repeat(64),
  schemaName: 'workspace_a',
  readonlyRole: 'workspace_a_ro',
};
const context = (changes = {}) => ({ securityContext: { ...base, ...changes } });

test('alternating workspaces never reuses another workspace driver', () => {
  // Mirrors Cube 1.6.39 getOrchestratorApi: cached orchestrator owns its driver.
  const drivers = new Map();
  const driverFor = (ctx) => {
    const key = config.contextToOrchestratorId(ctx);
    if (!drivers.has(key)) drivers.set(key, config.driverFactory(ctx));
    return drivers.get(key);
  };
  const a = context();
  const b = context({ workspaceId: 'workspace-b', schemaName: 'workspace_b', readonlyRole: 'workspace_b_ro' });
  const driverA = driverFor(a);
  const driverB = driverFor(b);
  assert.notEqual(driverA, driverB);
  assert.match(driverA.options, /role=workspace_a_ro .*search_path=workspace_a,public/);
  assert.match(driverB.options, /role=workspace_b_ro .*search_path=workspace_b,public/);
  assert.equal(driverFor(a), driverA);
  assert.equal(driverFor(b), driverB);
});

for (const [field, value] of Object.entries({
  workspaceId: 'another-workspace',
  schemaName: 'another_schema', readonlyRole: 'another_role',
})) {
  test(`both cache boundaries include ${field}`, () => {
    for (const key of ['contextToAppId', 'contextToOrchestratorId']) {
      assert.notEqual(config[key](context()), config[key](context({ [field]: value })));
    }
  });
}

test('model content invalidates compilation without multiplying connection pools', () => {
  for (const changed of [context({ cubeSchemaHash: 'b'.repeat(64) }), context({ semanticModelId: 'another-model' })]) {
    assert.notEqual(config.contextToAppId(context()), config.contextToAppId(changed));
    assert.equal(config.contextToOrchestratorId(context()), config.contextToOrchestratorId(changed));
  }
});

test('long schema names differing at the suffix remain distinct', () => {
  const prefix = 't_' + 'x'.repeat(53);
  const a = context({ schemaName: prefix + '_old', readonlyRole: 'same_read_role' });
  const b = context({ schemaName: prefix + '_new', readonlyRole: 'same_read_role' });
  for (const key of ['contextToAppId', 'contextToOrchestratorId']) {
    assert.notEqual(config[key](a), config[key](b));
    assert.match(config[key](a), /^scout_(app|data)_v1_[a-f0-9]{64}$/);
  }
});

test('tuple hashing does not collapse punctuation or use volatile JWT fields', () => {
  for (const key of ['contextToAppId', 'contextToOrchestratorId']) {
    assert.notEqual(config[key](context({ workspaceId: 'a-b' })), config[key](context({ workspaceId: 'a_b' })));
    assert.equal(config[key](context()), config[key](context({ userId: 'another-user', iat: 123, exp: 456 })));
  }
});

test('readiness has a separate context but partial tenant contexts fail closed', () => {
  for (const key of ['contextToAppId', 'contextToOrchestratorId']) {
    assert.equal(config[key]({}), 'scout_healthcheck');
    assert.equal(config[key]({ securityContext: {} }), 'scout_healthcheck');
    assert.notEqual(config[key](context()), 'scout_healthcheck');
  }
  for (const field of ['workspaceId', 'semanticModelId', 'schemaName', 'readonlyRole']) {
    for (const key of ['contextToAppId', 'contextToOrchestratorId', 'driverFactory']) {
      assert.throws(() => config[key](context({ [field]: '' })), /Cube security context/);
    }
  }
  assert.throws(() => config.driverFactory(context({ schemaName: 'tenant_a,public' })), /Invalid schemaName/);
  assert.throws(() => config.driverFactory(context({ readonlyRole: 'admin -c role=postgres' })), /Invalid readonlyRole/);
  assert.throws(() => config.driverFactory(context({ schemaName: ['workspace_a'] })), /Invalid schemaName/);
});

test('legacy JWTs receive the authoritative publication revision before SQL compilation', async () => {
  const calls = [];
  const revision = '2026-09-15T05:33:25.291945Z';
  const config = loadConfig(async (...args) => {
    calls.push(args);
    return { rows: [{ data_revision: revision }] };
  });
  const ctx = context();
  const query = { measures: ['visits.count'] };
  const orchestrator = config.contextToOrchestratorId(ctx);

  assert.equal(await config.queryRewrite(query, ctx), query);
  assert.equal(ctx.securityContext.cubeDataRevision, revision);
  assert.equal(config.contextToOrchestratorId(ctx), orchestrator);
  assert.deepEqual(Array.from(calls[0][1]), [base.workspaceId, base.semanticModelId]);
  assert.match(calls[0][0], /workspace_id = \$1/);
  assert.match(calls[0][0], /semantic_model_id = \$2/);
  assert.match(calls[0][0], /status = 'active'/);
  assert.match(calls[0][0], /updated_at AT TIME ZONE 'UTC'/);
  assert.match(calls[0][0], /SS\.US/);
});

test('same-YAML publications get fresh revisions without new orchestrators or driver scopes', async () => {
  let revision = '2026-09-15T05:33:25.291945Z';
  const config = loadConfig(async () => ({ rows: [{ data_revision: revision }] }));
  const before = context({ cubeDataRevision: 'caller-supplied-stale-value' });
  const unchanged = context();
  await config.queryRewrite({}, before);
  await config.queryRewrite({}, unchanged);
  assert.equal(before.securityContext.cubeDataRevision, revision);
  assert.equal(unchanged.securityContext.cubeDataRevision, revision);

  revision = '2026-09-15T05:33:25.291946Z';
  const after = context();
  await config.queryRewrite({}, after);
  assert.notEqual(after.securityContext.cubeDataRevision, before.securityContext.cubeDataRevision);
  assert.equal(config.contextToOrchestratorId(before), config.contextToOrchestratorId(after));
  assert.deepEqual(config.driverFactory(before), config.driverFactory(after));
});

test('one request shares one revision lookup across concurrent queries', async () => {
  let calls = 0;
  const config = loadConfig(async () => {
    calls += 1;
    return { rows: [{ data_revision: `publication-${calls}` }] };
  });
  const ctx = context();
  await Promise.all([config.queryRewrite({}, ctx), config.queryRewrite({}, ctx)]);
  assert.equal(calls, 1);
  assert.equal(ctx.securityContext.cubeDataRevision, 'publication-1');
});

test('publication lookup never trusts an old claim when the active schema is absent or unreadable', async () => {
  for (const query of [
    async () => ({ rows: [] }),
    async () => { throw new Error('Synthetic catalog unavailable'); },
  ]) {
    const config = loadConfig(query);
    await assert.rejects(config.queryRewrite({}, context({ cubeDataRevision: 'old' })));
  }
});

test('publication lookup preserves health checks and rejects partial tenant contexts before database access', async () => {
  const query = { measures: [] };
  assert.equal(await config.queryRewrite(query, {}), query);
  assert.equal(await config.queryRewrite(query, { securityContext: {} }), query);
  for (const field of ['workspaceId', 'semanticModelId', 'schemaName', 'readonlyRole']) {
    await assert.rejects(config.queryRewrite(query, context({ [field]: '' })), /Cube security context/);
  }
});

test('catalog connection acquisition and query execution have finite time budgets', async () => {
  const pools = [];
  const config = loadConfig(async () => ({ rows: [{ data_revision: 'r' }] }), pools);
  await config.queryRewrite({}, context());
  assert.equal(pools.length, 2);
  for (const pool of pools) {
    for (const option of ['connectionTimeoutMillis', 'statement_timeout', 'query_timeout']) {
      assert.equal(pool[option], 5000);
    }
  }
});

test('catalog reads run as the SELECT-only role once it exists, and never fall back', async () => {
  const pools = [];
  const seen = [];
  const catalog = { roleExists: true, probes: 0, warnings: [] };
  const config = loadConfig(async (text, values, options) => {
    seen.push(options);
    return { rows: [{ data_revision: 'r', filename: 'f', content: 'c', content_hash: 'h', updated_at: new Date(0) }] };
  }, pools, {}, catalog);
  await config.queryRewrite({}, context());
  await config.repositoryFactory(context()).dataSchemaFiles();
  catalog.roleExists = false;
  await config.schemaVersion(context());
  assert.equal(catalog.probes, 1);
  assert.equal(seen.length, 3);
  for (const options of seen) {
    assert.equal(options.options, '-c role=scout_cube_catalog -c default_transaction_read_only=on');
    assert.equal(options.max, 3);
  }
  assert.deepEqual(catalog.warnings, []);
});

test('before the migration creates the role, catalog reads use the owner and re-probe later', async () => {
  const seen = [];
  const catalog = { roleExists: false, probes: 0, warnings: [] };
  const config = loadConfig(async (text, values, options) => {
    seen.push(options);
    return { rows: [{ data_revision: 'r' }] };
  }, [], {}, catalog);
  await config.queryRewrite({}, context());
  await config.queryRewrite({}, context());
  assert.equal(catalog.probes, 1, 'a missing role is not re-probed on every request');
  assert.equal(catalog.warnings.length, 1);
  assert.match(catalog.warnings[0], /scout_cube_catalog cannot read semantic_cubeschema yet/);
  for (const options of seen) {
    assert.equal(options.options, undefined);
    assert.equal(options.max, 3);
  }
});

test('the readiness driver is time-bounded, read-only, and cannot resolve tenant tables', () => {
  for (const ctx of [{}, { securityContext: {} }]) {
    const driver = config.driverFactory(ctx);
    assert.match(driver.options ?? '', /-c statement_timeout=30000(\s|$)/);
    assert.match(driver.options ?? '', /-c default_transaction_read_only=on(\s|$)/);
    assert.match(driver.options ?? '', /-c search_path=pg_catalog(\s|$)/);
  }
});

test('every pool is capped and sheds idle connections', () => {
  const pools = [];
  const config = loadConfig(undefined, pools);
  assert.equal(pools[0].max, 3);
  for (const [ctx, max] of [[context(), 2], [{}, 1]]) {
    const driver = config.driverFactory(ctx);
    assert.equal(driver.config.maxPoolSize, max);
    assert.equal(driver.config.idleTimeoutMillis, 10000);
    assert.equal(driver.config.softIdleTimeoutMillis, 10000);
    assert.equal(driver.config.dataSource, 'default');
  }
});

test('tenant connections share one process-wide limit across workspaces', async () => {
  const config = loadConfig(undefined, [], { SCOUT_CUBE_MAX_DRIVER_CONNECTIONS: '2' });
  const a = config.driverFactory(context());
  const b = config.driverFactory(context({ workspaceId: 'workspace-b', schemaName: 'workspace_b', readonlyRole: 'workspace_b_ro' }));
  const first = await a.createConnection();
  await b.createConnection();
  let third = null;
  const waiting = b.createConnection().then((client) => { third = client; });
  await new Promise(setImmediate);
  assert.equal(third, null);

  first.end();
  first.end();
  await waiting;
  assert.ok(third);
  let fourth = null;
  const pendingFourth = a.createConnection().then((client) => { fourth = client; });
  await new Promise(setImmediate);
  assert.equal(fourth, null, 'a repeated end must not free a second slot');
  third.end();
  await pendingFourth;
  assert.ok(fourth);
});

test('a failed connect returns its slot', async () => {
  const config = loadConfig(undefined, [], { SCOUT_CUBE_MAX_DRIVER_CONNECTIONS: '1' });
  const failing = config.driverFactory(context());
  failing.config.failConnect = true;
  await assert.rejects(failing.createConnection(), /Synthetic connect failure/);
  failing.config.failConnect = false;
  assert.ok(await failing.createConnection());
});

test('readiness connections stay outside the tenant limit', async () => {
  const config = loadConfig(undefined, [], { SCOUT_CUBE_MAX_DRIVER_CONNECTIONS: '1' });
  await config.driverFactory(context()).createConnection();
  assert.ok(await config.driverFactory({}).createConnection());
});

test('a failed role check falls back to the owner instead of failing the read', async () => {
  const seen = [];
  const catalog = { roleExists: true, probeError: true, probes: 0, warnings: [] };
  const config = loadConfig(async (text, values, options) => {
    seen.push(options);
    return { rows: [{ data_revision: 'r' }] };
  }, [], {}, catalog);
  await config.queryRewrite({}, context());
  await config.queryRewrite({}, context());
  assert.equal(seen.length, 2);
  assert.equal(seen[0].options, undefined);
  assert.equal(catalog.warnings.length, 1, 'a failed check backs off instead of re-probing every request');
  assert.match(catalog.warnings[0], /synthetic probe failure/);
});

test('a slot wait is bounded and a timed-out waiter leaves the queue', async () => {
  const { createConnectionSlots } = require('./connection-slots');
  const timers = [];
  const slots = createConnectionSlots(1, 50, {
    setTimeout: (callback) => { const timer = { callback }; timers.push(timer); return timer; },
    clearTimeout: (timer) => { timer.cleared = true; },
  });
  const release = await slots.acquire();
  const expired = slots.acquire();
  const waiting = slots.acquire();
  timers[0].callback();
  await assert.rejects(expired, /No Cube database connection slot freed within 50ms \(limit 1\)/);
  release();
  const next = await waiting;
  assert.equal(timers[1].cleared, true);
  next();
  assert.ok(await slots.acquire());
});

test('a driver without the createConnection hook fails startup instead of leaving the limit inert', () => {
  class HooklessDriver {}
  assert.throws(() => loadConfig(undefined, [], {}, undefined, HooklessDriver), /tenant connection limit would be inert/);
});

test('an invalid tenant connection limit fails startup', () => {
  for (const value of ['0', '-1', '1.5', 'many', '9007199254740993']) {
    assert.throws(() => loadConfig(undefined, [], { SCOUT_CUBE_MAX_DRIVER_CONNECTIONS: value }), /positive safe integer/);
  }
});
