'use strict';

const assert = require('node:assert/strict');
const { join } = require('node:path');
const { applyPinnedPatch, runPatchCli, PINNED_VERSION } = require('./pinned-patch');

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
  const { servingRequire, target, result } = applyPinnedPatch({
    serverRoot,
    packageName: '@cubejs-backend/server-core',
    targetPath: 'dist/src/core/OrchestratorStorage.js',
    originalSha256: ORIGINAL_SHA256,
    patchedSource: PATCHED_SOURCE,
    installedModuleName: IDLE_MODULE,
    sourcePath,
    verifyOnly,
    label: 'OrchestratorStorage',
  });

  const { OrchestratorStorage } = servingRequire(target);
  const storage = new OrchestratorStorage();
  // The patch replaces the whole upstream module, so check everything server-core calls.
  for (const method of UPSTREAM_METHODS) {
    assert.equal(typeof storage[method], 'function', `Patched orchestrator storage is missing ${method}()`);
  }
  assert.ok(storage.idleTtlMs > 0, 'Serving OrchestratorStorage did not load the idle-expiring storage');
  return result;
}

if (require.main === module) {
  runPatchCli(
    'patch-orchestrator-storage.js',
    patchOrchestratorStorage,
    `Verified idle-expiring Cube ${PINNED_VERSION} orchestrator storage`
  );
}

module.exports = { patchOrchestratorStorage, PINNED_VERSION, ORIGINAL_SHA256, PATCHED_SOURCE, UPSTREAM_METHODS };
