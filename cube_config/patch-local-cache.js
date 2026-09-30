'use strict';

const assert = require('node:assert/strict');
const { join } = require('node:path');
const { applyPinnedPatch, runPatchCli, PINNED_VERSION, sha256 } = require('./pinned-patch');

const ORIGINAL_SHA256 = '4ae2bb338575d32846ee67fd0fb71c30afb0f04c585afe19d78430ffb0c4700d';
const BOUNDED_MODULE = 'ScoutBoundedLocalCache.js';
const PATCHED_SOURCE = `"use strict";
// Scout build-time patch: pinned Cube ${PINNED_VERSION}; verified before install/start.
Object.defineProperty(exports, "__esModule", { value: true });
const { createCancelablePromise } = require("@cubejs-backend/shared");
const { createLocalCacheDriver } = require("./${BOUNDED_MODULE}");
exports.LocalCacheDriver = createLocalCacheDriver({ createCancelablePromise });
`;

function patchLocalCache({ serverRoot = '/cube', sourcePath = join(__dirname, 'bounded-local-cache.js'), verifyOnly = false } = {}) {
  const { servingRequire, target, result } = applyPinnedPatch({
    serverRoot,
    packageName: '@cubejs-backend/query-orchestrator',
    targetPath: 'dist/src/orchestrator/LocalCacheDriver.js',
    originalSha256: ORIGINAL_SHA256,
    patchedSource: PATCHED_SOURCE,
    installedModuleName: BOUNDED_MODULE,
    sourcePath,
    verifyOnly,
    label: 'LocalCacheDriver',
  });

  const { LocalCacheDriver } = servingRequire(target);
  const { QueryCache } = servingRequire('@cubejs-backend/query-orchestrator');
  const queryCache = new QueryCache('scout_patch_verification', async () => {
    throw new Error('Cache patch verification must not access a database');
  }, () => {}, { cacheAndQueueDriver: 'memory' });
  assert(queryCache.getCacheDriver() instanceof LocalCacheDriver, 'Serving QueryCache did not load the patched driver');
  assert.equal(typeof queryCache.getCacheDriver().getStats, 'function', 'Patched cache API is missing');
  return result;
}

if (require.main === module) {
  runPatchCli('patch-local-cache.js', patchLocalCache, `Verified bounded Cube ${PINNED_VERSION} query-result cache`);
}

module.exports = { patchLocalCache, PINNED_VERSION, ORIGINAL_SHA256, PATCHED_SOURCE, sha256 };
