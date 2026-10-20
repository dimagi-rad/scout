"""The validator's /version reports the compiler actually installed in the Cube image.

Scout skips revalidating an unchanged Cube schema only while this version matches
the one that last passed it (#918), so a stale answer would hide a Cube upgrade.
Requires SCOUT_CUBE_COMPILER_CONTAINER naming a local container of the Cube image.
"""

import json
import os
import shutil
import subprocess

import pytest

_VERIFY = r"""
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { createValidatorApp } = require('/cube/conf/validator-core');

async function run() {
  const compilerDir = path.dirname(require.resolve('@cubejs-backend/schema-compiler'));
  let dir = compilerDir;
  let installed;
  while (dir !== path.dirname(dir)) {
    try {
      const pkg = JSON.parse(readFileSync(path.join(dir, 'package.json'), 'utf8'));
      if (pkg.name === '@cubejs-backend/schema-compiler') { installed = pkg.version; break; }
    } catch (error) { /* keep walking up */ }
    dir = path.dirname(dir);
  }
  assert.ok(installed, 'installed schema-compiler package.json not found');

  const app = createValidatorApp({
    compileSchema: async () => { throw new Error('/version must not compile'); },
    redis: null,
    logger: false,
    authSecret: 'unused',
  });
  const response = await app.inject({ method: 'GET', url: '/version' });
  assert.equal(response.statusCode, 200);
  const body = response.json();
  assert.equal(body.compiler_version, installed);
  assert.ok(body.validator_version);
  process.stdout.write(JSON.stringify(body));
  await app.close();
}

run().catch((error) => { console.error(error); process.exit(1); });
"""


@pytest.mark.smoke
def test_validator_version_is_the_installed_compiler():
    container = os.environ.get("SCOUT_CUBE_COMPILER_CONTAINER")
    if not container:
        pytest.skip("Set SCOUT_CUBE_COMPILER_CONTAINER to a local Cube container.")
    docker = shutil.which("docker")
    assert docker, "The Cube validator version smoke test requires Docker."
    result = subprocess.run(  # noqa: S603
        [docker, "exec", "-w", "/cube/conf", container, "node", "-e", _VERIFY],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert set(body) == {"compiler_version", "validator_version"}
