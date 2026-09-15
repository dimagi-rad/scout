'use strict';

const assert = require('node:assert/strict');
const { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { dirname, join } = require('node:path');
const { test } = require('node:test');
const { patchLocalCache, ORIGINAL_SHA256, PATCHED_SOURCE, sha256 } = require('./patch-local-cache');

// Exact upstream 1.6.39 installed CommonJS artifact, not a permissive mock hash.
const ORIGINAL_SOURCE = `"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.LocalCacheDriver = void 0;
const shared_1 = require("@cubejs-backend/shared");
const store = {};
class LocalCacheDriver {
    store;
    constructor() {
        this.store = store;
    }
    async get(key) {
        if (this.store[key] && this.store[key].exp < new Date().getTime()) {
            delete this.store[key];
        }
        return this.store[key] && this.store[key].value;
    }
    async set(key, value, expiration) {
        this.store[key] = {
            value,
            exp: new Date().getTime() + expiration * 1000
        };
        return {
            key,
            bytes: Buffer.byteLength(JSON.stringify(value)),
        };
    }
    async remove(key) {
        delete this.store[key];
    }
    async keysStartingWith(prefix) {
        return Object.keys(this.store)
            .filter(k => k.indexOf(prefix) === 0 && this.store[k].exp > new Date().getTime());
    }
    async cleanup() {
        // Nothing to do
    }
    reset() {
        for (const key of Object.keys(this.store)) {
            delete this.store[key];
        }
    }
    async testConnection() {
        // Nothing to do
    }
    withLock = (key, cb, expiration = 60, freeAfter = true) => (0, shared_1.createCancelablePromise)(async (tkn) => {
        if (key in this.store) {
            if (this.store[key].exp < new Date().getTime()) {
                delete this.store[key];
            }
            return false;
        }
        try {
            this.store[key] = {
                value: Math.random(),
                exp: new Date().getTime() + expiration * 1000
            };
            await tkn.with(cb());
            return true;
        }
        finally {
            if (freeAfter) {
                delete this.store[key];
            }
        }
    });
}
exports.LocalCacheDriver = LocalCacheDriver;
//# sourceMappingURL=LocalCacheDriver.js.map`;

function write(path, value) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, value);
}

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'scout-cache-patch-'));
  t.after(() => { rmSync(root, { recursive: true, force: true }); });
  const packageRoot = join(root, 'node_modules/@cubejs-backend/query-orchestrator');
  const packagePath = join(packageRoot, 'package.json');
  const target = join(packageRoot, 'dist/src/orchestrator/LocalCacheDriver.js');
  const installedModule = join(dirname(target), 'ScoutBoundedLocalCache.js');
  write(packagePath, JSON.stringify({ version: '1.6.39', main: 'index.js' }));
  write(target, ORIGINAL_SOURCE);
  write(join(packageRoot, 'index.js'), `
    const { LocalCacheDriver } = require('./dist/src/orchestrator/LocalCacheDriver');
    exports.QueryCache = class {
      constructor() { this.driver = new LocalCacheDriver(); }
      getCacheDriver() { return this.driver; }
    };
  `);
  write(join(root, 'node_modules/@cubejs-backend/shared/index.js'), `
    exports.createCancelablePromise = fn => fn({ with: promise => promise });
  `);
  write(join(root, 'node_modules/@cubejs-backend/server/bin/server'), '// fixture resolver only');
  mkdirSync(join(root, 'node_modules/.bin'), { recursive: true });
  symlinkSync('../@cubejs-backend/server/bin/server', join(root, 'node_modules/.bin/cubejs-server'));
  return { root, packageRoot, packagePath, target, installedModule };
}

test('upstream fixture matches the exact installed-artifact integrity guard', () => {
  assert.equal(sha256(ORIGINAL_SOURCE), ORIGINAL_SHA256);
});

test('patch installs and verifies through the actual serving resolver', t => {
  const f = fixture(t);
  const result = patchLocalCache({ serverRoot: f.root });
  assert.equal(result.version, '1.6.39');
  assert.equal(readFileSync(f.target, 'utf8'), PATCHED_SOURCE);
  assert.equal(result.moduleHash, sha256(readFileSync(join(__dirname, 'bounded-local-cache.js'))));
  assert.deepEqual(patchLocalCache({ serverRoot: f.root, verifyOnly: true }), result);
  assert.deepEqual(patchLocalCache({ serverRoot: f.root }), result);
});

test('version drift fails before installing or overwriting anything', t => {
  const f = fixture(t);
  writeFileSync(f.packagePath, JSON.stringify({ version: '1.6.40', main: 'index.js' }));
  assert.throws(() => patchLocalCache({ serverRoot: f.root }), /before upgrading/);
  assert.equal(readFileSync(f.target, 'utf8'), ORIGINAL_SOURCE);
  assert.equal(existsSync(f.installedModule), false);
});

test('even whitespace drift in the pinned target fails closed', t => {
  const f = fixture(t);
  writeFileSync(f.target, ORIGINAL_SOURCE + '\n');
  assert.throws(() => patchLocalCache({ serverRoot: f.root }), /content drifted/);
  assert.equal(readFileSync(f.target, 'utf8'), ORIGINAL_SOURCE + '\n');
  assert.equal(existsSync(f.installedModule), false);
});

test('startup verification does not silently install a missing patch', t => {
  const f = fixture(t);
  assert.throws(() => patchLocalCache({ serverRoot: f.root, verifyOnly: true }), /patch is missing/);
  assert.equal(readFileSync(f.target, 'utf8'), ORIGINAL_SOURCE);
  assert.equal(existsSync(f.installedModule), false);
});

test('startup verification rejects a modified installed bounded module', t => {
  const f = fixture(t);
  patchLocalCache({ serverRoot: f.root });
  writeFileSync(f.installedModule, readFileSync(f.installedModule, 'utf8') + '\n');
  assert.throws(() => patchLocalCache({ serverRoot: f.root, verifyOnly: true }), /module content drifted/);
});

test('startup verification rejects a modified patched entrypoint', t => {
  const f = fixture(t);
  patchLocalCache({ serverRoot: f.root });
  writeFileSync(f.target, PATCHED_SOURCE + '\n');
  assert.throws(() => patchLocalCache({ serverRoot: f.root, verifyOnly: true }), /content drifted/);
});

test('validator dependency copy stays untouched while serving copy is patched', t => {
  const f = fixture(t);
  const decoy = join(f.root, 'conf/node_modules/@cubejs-backend/query-orchestrator/dist/src/orchestrator/LocalCacheDriver.js');
  write(decoy, 'validator-only decoy');
  patchLocalCache({ serverRoot: f.root });
  assert.equal(readFileSync(decoy, 'utf8'), 'validator-only decoy');
  assert.equal(readFileSync(f.target, 'utf8'), PATCHED_SOURCE);
});

test('a newly nested serving package resolution is rejected rather than patching the wrong copy', t => {
  const f = fixture(t);
  const nested = join(f.root, 'node_modules/@cubejs-backend/server/node_modules/@cubejs-backend/query-orchestrator');
  write(join(nested, 'package.json'), JSON.stringify({ version: '1.6.39', main: 'index.js' }));
  write(join(nested, 'index.js'), 'module.exports = {}');
  assert.throws(() => patchLocalCache({ serverRoot: f.root }), /resolution drifted/);
  assert.equal(readFileSync(f.target, 'utf8'), ORIGINAL_SOURCE);
});

test('image build and both startup paths validate the pinned package patch', () => {
  const dockerfile = readFileSync(join(__dirname, 'Dockerfile'), 'utf8');
  assert.match(dockerfile, /^FROM cubejs\/cube:v1\.6\.39$/m);
  assert.match(dockerfile, /^COPY bounded-local-cache\.js \/cube\/conf\/bounded-local-cache\.js$/m);
  assert.match(dockerfile, /^COPY patch-local-cache\.js \/cube\/conf\/patch-local-cache\.js$/m);
  assert.match(dockerfile, /&& node \/cube\/conf\/patch-local-cache\.js \\/);
  assert.match(dockerfile, /CMD .*patch-local-cache\.js --verify && exec \/start-cube-with-validator\.sh/);
  const packageJson = JSON.parse(readFileSync(join(__dirname, 'package.json'), 'utf8'));
  assert.equal(packageJson.scripts.start,
    'node /cube/conf/patch-local-cache.js --verify && /start-cube-with-validator.sh');
});
