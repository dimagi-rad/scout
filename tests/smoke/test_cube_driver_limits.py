"""DB-free checks that Cube drivers stay bounded and verify TLS inside the real image.

Requires SCOUT_CUBE_COMPILER_CONTAINER naming an existing local Cube container. The
script loads /cube/conf/cube.js in a separate Node process with an unreachable database,
so the serving process and all databases are untouched.
"""

import json
import os
import shutil
import subprocess

import pytest

_VERIFY = r"""
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { realpathSync } = require('node:fs');
const serverRequire = createRequire(realpathSync('/cube/node_modules/.bin/cubejs-server'));
const { isDriver } = serverRequire('@cubejs-backend/server-core/dist/src/core/DriverResolvers');
const { OrchestratorStorage } = serverRequire('@cubejs-backend/server-core/dist/src/core/OrchestratorStorage');
const config = require('/cube/conf/cube.js');

const tenant = {
  securityContext: {
    workspaceId: '11111111-1111-4111-8111-111111111111',
    semanticModelId: '22222222-2222-4222-8222-222222222222',
    schemaName: 'workspace_a',
    readonlyRole: 'workspace_a_ro',
  },
  dataSource: 'default',
};

async function run() {
  assert.equal(new OrchestratorStorage().idleTtlMs, 600000);
  const driver = config.driverFactory(tenant);
  const readiness = config.driverFactory({ dataSource: 'default' });
  assert.equal(isDriver(driver), true);
  assert.equal(isDriver(readiness), true);
  assert.equal(driver.pool.max, 2);
  assert.equal(readiness.pool.max, 1);
  // The tenant limit overrides createConnection(); the pinned driver's pool
  // factory must still route through it or the limit is inert.
  assert.match(driver.pool._factory.create.toString(), /this\.createConnection\(/);
  // The pinned driver must pass these through to generic-pool, not hard-code them.
  for (const [pool, max] of [[driver.pool, 2], [readiness.pool, 1]]) {
    const options = pool.pool._config;
    assert.equal(options.max, max);
    assert.equal(options.min, 0);
    assert.equal(options.idleTimeoutMillis, 10000);
    assert.equal(options.softIdleTimeoutMillis, 10000);
    assert.equal(options.evictionRunIntervalMillis, 5000);
    assert.equal(options.acquireTimeoutMillis, 20000);
  }
  // With one tenant slot, a leaked slot from the first refused connect would
  // leave the second waiting forever instead of failing fast.
  for (let attempt = 0; attempt < 2; attempt += 1) {
    let timer;
    const leaked = new Promise((resolve) => {
      timer = setTimeout(() => resolve('A refused tenant connect did not return its slot'), 5000);
    });
    const outcome = await Promise.race([
      driver.testConnection().then(() => 'connected', () => 'refused'),
      leaked,
    ]);
    clearTimeout(timer);
    assert.equal(outcome, 'refused');
  }
  await driver.release();
  await readiness.release();
  process.stdout.write(JSON.stringify({
    tenantMax: 2, readinessMax: 1, slotReleased: true, idleOrchestratorsExpire: true,
  }));
}
run().then(() => process.exit(0)).catch(error => { console.error(error); process.exit(1); });
"""


@pytest.mark.smoke
def test_real_cube_drivers_are_bounded_and_return_slots():
    container = os.environ.get("SCOUT_CUBE_COMPILER_CONTAINER")
    if not container:
        pytest.skip("Set SCOUT_CUBE_COMPILER_CONTAINER to a local Cube container.")
    docker = shutil.which("docker")
    assert docker, "The real Cube driver smoke test requires Docker."
    unreachable = "postgresql://unused:unused@127.0.0.1:1/unused"
    result = subprocess.run(  # noqa: S603
        [
            docker,
            "exec",
            "-e",
            f"DATABASE_URL={unreachable}",
            "-e",
            f"MANAGED_DATABASE_URL={unreachable}",
            "-e",
            "SCOUT_CUBE_MAX_DRIVER_CONNECTIONS=1",
            "-w",
            "/cube/conf",
            container,
            "node",
            "-e",
            _VERIFY,
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "tenantMax": 2,
        "readinessMax": 1,
        "slotReleased": True,
        "idleOrchestratorsExpire": True,
    }


_VERIFY_TLS = r"""
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const pg = require('pg');
const ConnectionParameters = require('pg/lib/connection-parameters');
const poolOptions = [];
const RealPool = pg.Pool;
pg.Pool = class extends RealPool {
  constructor(options) { super(options); poolOptions.push(options); }
};
const config = require('/cube/conf/cube.js');
const ca = readFileSync('/cube/conf/rds-global-bundle.pem', 'utf8');
const effective = [
  ...poolOptions.map((options) => new ConnectionParameters(options).ssl),
  config.driverFactory({}).config.ssl,
];
for (const ssl of effective) {
  assert.equal(ssl.rejectUnauthorized, true);
  assert.equal(ssl.ca, ca);
}
process.stdout.write(JSON.stringify({ verified: effective.length }));
"""


@pytest.mark.smoke
def test_real_cube_image_verifies_remote_database_certificates():
    container = os.environ.get("SCOUT_CUBE_COMPILER_CONTAINER")
    if not container:
        pytest.skip("Set SCOUT_CUBE_COMPILER_CONTAINER to a local Cube container.")
    docker = shutil.which("docker")
    assert docker, "The real Cube TLS smoke test requires Docker."
    # .invalid never resolves, and building pools and drivers opens no connection.
    remote = "postgresql://unused:unused@db.example.invalid:5432/unused?sslmode=no-verify"
    result = subprocess.run(  # noqa: S603
        [
            docker,
            "exec",
            "-e",
            f"DATABASE_URL={remote}",
            "-e",
            f"MANAGED_DATABASE_URL={remote}",
            "-w",
            "/cube/conf",
            container,
            "node",
            "-e",
            _VERIFY_TLS,
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"verified": 2}
