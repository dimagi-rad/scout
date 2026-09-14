const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

// Exercise the production configuration without a network connection or npm
// install. Only pg.Pool is stubbed; ID generation and driver configuration run.
const sandbox = {
  module: { exports: {} },
  process: { env: {} },
  URL,
  require: (name) => name === 'pg' ? { Pool: class {} } : require(name),
};
vm.runInNewContext(readFileSync(join(__dirname, 'cube.js'), 'utf8'), sandbox);
const config = sandbox.module.exports;
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
