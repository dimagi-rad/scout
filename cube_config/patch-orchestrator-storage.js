'use strict';

const assert = require('node:assert/strict');
const { readFileSync, realpathSync, writeFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const { dirname, join, resolve } = require('node:path');
const { sha256 } = require('./patch-local-cache');

const PINNED_VERSION = '1.6.39';
const ORIGINAL_SHA256 = '164a5d088b7f494f4f3e42755d8ee5b1b0b8b2a3cebf4eecddcc84bc7a740b9f';
const IDLE_MODULE = 'ScoutIdleOrchestratorStorage.js';
const UPSTREAM_METHODS = Object.freeze([
  'has', 'get', 'set', 'clear', 'testConnections', 'testOrchestratorConnections', 'releaseConnections',
]);
const PATCHED_SOURCE = `"use strict";
// Scout build-time patch: pinned Cube ${PINNED_VERSION}; verified before install/start.
Object.defineProperty(exports, "__esModule", { value: true });
const { IdleOrchestratorStorage } = require("./${IDLE_MODULE}");
exports.OrchestratorStorage = IdleOrchestratorStorage;
`;

function patchOrchestratorStorage({ serverRoot = '/cube', sourcePath = join(__dirname, 'idle-orchestrator-storage.js'), verifyOnly = false } = {}) {
  const serverEntry = realpathSync(join(serverRoot, 'node_modules/.bin/cubejs-server'));
  const servingRequire = createRequire(serverEntry);
  const packagePath = servingRequire.resolve('@cubejs-backend/server-core/package.json');
  const expectedPackage = resolve(serverRoot, 'node_modules/@cubejs-backend/server-core/package.json');
  assert.equal(realpathSync(packagePath), realpathSync(expectedPackage), 'Serving Cube package resolution drifted');
  const version = JSON.parse(readFileSync(packagePath, 'utf8')).version;
  assert.equal(version, PINNED_VERSION, 'Review the orchestrator patch before upgrading Cube');
  const target = join(dirname(packagePath), 'dist/src/core/OrchestratorStorage.js');
  const installedModule = join(dirname(target), IDLE_MODULE);
  const originalHash = sha256(readFileSync(target));
  const patchedHash = sha256(PATCHED_SOURCE);
  const moduleSource = readFileSync(sourcePath);
  if (originalHash === ORIGINAL_SHA256 && !verifyOnly) {
    writeFileSync(installedModule, moduleSource);
    writeFileSync(target, PATCHED_SOURCE);
  } else {
    assert.equal(originalHash, patchedHash, 'Pinned OrchestratorStorage content drifted or patch is missing');
    assert.equal(sha256(readFileSync(installedModule)), sha256(moduleSource), 'Idle orchestrator module content drifted');
  }
  assert.equal(sha256(readFileSync(target)), patchedHash, 'Orchestrator patch installation failed');
  assert.equal(sha256(readFileSync(installedModule)), sha256(moduleSource), 'Orchestrator module installation failed');

  const { OrchestratorStorage } = servingRequire(target);
  const storage = new OrchestratorStorage();
  // The patch replaces the whole upstream module, so check everything server-core calls.
  for (const method of UPSTREAM_METHODS) {
    assert.equal(typeof storage[method], 'function', `Patched orchestrator storage is missing ${method}()`);
  }
  assert.ok(storage.idleTtlMs > 0, 'Serving OrchestratorStorage did not load the idle-expiring storage');
  return { version, targetHash: patchedHash, moduleHash: sha256(moduleSource) };
}

if (require.main === module) {
  const args = process.argv.slice(2);
  const options = {};
  while (args.length) {
    const arg = args.shift();
    if (arg === '--verify') options.verifyOnly = true;
    else if (arg === '--server-root' && args[0]) options.serverRoot = args.shift();
    else throw new Error('Usage: patch-orchestrator-storage.js [--verify] [--server-root PATH]');
  }
  patchOrchestratorStorage(options);
  process.stdout.write(`Verified idle-expiring Cube ${PINNED_VERSION} orchestrator storage\n`);
}

module.exports = { patchOrchestratorStorage, PINNED_VERSION, ORIGINAL_SHA256, PATCHED_SOURCE, UPSTREAM_METHODS };
