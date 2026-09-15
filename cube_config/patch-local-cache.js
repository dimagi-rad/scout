'use strict';

const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { readFileSync, realpathSync, writeFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const { dirname, join, resolve } = require('node:path');

const PINNED_VERSION = '1.6.39';
const ORIGINAL_SHA256 = '4ae2bb338575d32846ee67fd0fb71c30afb0f04c585afe19d78430ffb0c4700d';
const BOUNDED_MODULE = 'ScoutBoundedLocalCache.js';
const PATCHED_SOURCE = `"use strict";
// Scout build-time patch: pinned Cube ${PINNED_VERSION}; verified before install/start.
Object.defineProperty(exports, "__esModule", { value: true });
const { createCancelablePromise } = require("@cubejs-backend/shared");
const { createLocalCacheDriver } = require("./${BOUNDED_MODULE}");
exports.LocalCacheDriver = createLocalCacheDriver({ createCancelablePromise });
`;

function sha256(content) {
  return createHash('sha256').update(content).digest('hex');
}

function patchLocalCache({ serverRoot = '/cube', sourcePath = join(__dirname, 'bounded-local-cache.js'), verifyOnly = false } = {}) {
  // Resolve from the actual serving CLI, never the validator's /cube/conf tree.
  const serverEntry = realpathSync(join(serverRoot, 'node_modules/.bin/cubejs-server'));
  const servingRequire = createRequire(serverEntry);
  const packagePath = servingRequire.resolve('@cubejs-backend/query-orchestrator/package.json');
  const expectedPackage = resolve(serverRoot, 'node_modules/@cubejs-backend/query-orchestrator/package.json');
  assert.equal(realpathSync(packagePath), realpathSync(expectedPackage), 'Serving Cube package resolution drifted');
  const version = JSON.parse(readFileSync(packagePath, 'utf8')).version;
  assert.equal(version, PINNED_VERSION, 'Review the cache patch before upgrading Cube');
  const target = join(dirname(packagePath), 'dist/src/orchestrator/LocalCacheDriver.js');
  const installedModule = join(dirname(target), BOUNDED_MODULE);
  const originalHash = sha256(readFileSync(target));
  const patchedHash = sha256(PATCHED_SOURCE);
  const moduleSource = readFileSync(sourcePath);
  if (originalHash === ORIGINAL_SHA256 && !verifyOnly) {
    writeFileSync(installedModule, moduleSource);
    writeFileSync(target, PATCHED_SOURCE);
  } else {
    assert.equal(originalHash, patchedHash, 'Pinned LocalCacheDriver content drifted or patch is missing');
    assert.equal(sha256(readFileSync(installedModule)), sha256(moduleSource), 'Bounded cache module content drifted');
  }
  assert.equal(sha256(readFileSync(target)), patchedHash, 'Cache patch installation failed');
  assert.equal(sha256(readFileSync(installedModule)), sha256(moduleSource), 'Cache module installation failed');

  const { LocalCacheDriver } = servingRequire(target);
  const { QueryCache } = servingRequire('@cubejs-backend/query-orchestrator');
  const queryCache = new QueryCache('scout_patch_verification', async () => {
    throw new Error('Cache patch verification must not access a database');
  }, () => {}, { cacheAndQueueDriver: 'memory' });
  assert(queryCache.getCacheDriver() instanceof LocalCacheDriver, 'Serving QueryCache did not load the patched driver');
  assert.equal(typeof queryCache.getCacheDriver().getStats, 'function', 'Patched cache API is missing');
  return { version, targetHash: patchedHash, moduleHash: sha256(moduleSource) };
}

if (require.main === module) {
  const args = process.argv.slice(2);
  const options = {};
  while (args.length) {
    const arg = args.shift();
    if (arg === '--verify') options.verifyOnly = true;
    else if (arg === '--server-root' && args[0]) options.serverRoot = args.shift();
    else throw new Error('Usage: patch-local-cache.js [--verify] [--server-root PATH]');
  }
  patchLocalCache(options);
  process.stdout.write(`Verified bounded Cube ${PINNED_VERSION} query-result cache\n`);
}

module.exports = { patchLocalCache, PINNED_VERSION, ORIGINAL_SHA256, PATCHED_SOURCE, sha256 };
