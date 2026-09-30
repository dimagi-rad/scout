"""DB-free check that Cube drivers and orchestrators stay bounded inside the real image.

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
