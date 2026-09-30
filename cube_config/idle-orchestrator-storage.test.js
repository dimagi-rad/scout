'use strict';

const assert = require('node:assert/strict');
const { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { dirname, join } = require('node:path');
const { test } = require('node:test');
const { IdleOrchestratorStorage } = require('./idle-orchestrator-storage');
const { patchOrchestratorStorage, ORIGINAL_SHA256, PATCHED_SOURCE, UPSTREAM_METHODS } = require('./patch-orchestrator-storage');
const { sha256 } = require('./pinned-patch');

// Exact upstream 1.6.39 installed CommonJS artifact, not a permissive mock hash.
const ORIGINAL_SOURCE = `"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.OrchestratorStorage = void 0;
const lru_cache_1 = require("lru-cache");
class OrchestratorStorage {
    storage;
    constructor(options = { compilerCacheSize: 100 }) {
        this.storage = new lru_cache_1.LRUCache({
            max: options.compilerCacheSize,
            ttl: options.maxCompilerCacheKeepAlive,
            updateAgeOnGet: options.updateCompilerCacheKeepAlive
        });
    }
    has(orchestratorId) {
        return this.storage.has(orchestratorId);
    }
    get(orchestratorId) {
        return this.storage.get(orchestratorId);
    }
    set(orchestratorId, orchestratorApi) {
        return this.storage.set(orchestratorId, orchestratorApi);
    }
    clear() {
        this.storage.clear();
    }
    async testConnections() {
        return Promise.all([...this.storage.values()].map(api => api.testConnection()));
    }
    async testOrchestratorConnections() {
        return Promise.all([...this.storage.values()].map(api => api.testOrchestratorConnections()));
    }
    async releaseConnections() {
        await Promise.all([...this.storage.values()].map(api => api.release()));
        this.storage.clear();
    }
}
exports.OrchestratorStorage = OrchestratorStorage;
//# sourceMappingURL=OrchestratorStorage.js.map`;

function clock() {
  let time = 0;
  const timers = [];
  return {
    now: () => time,
    advance(ms) { time += ms; },
    setTimeout: (callback, ms) => { const timer = { callback, at: time + ms, cancelled: false }; timers.push(timer); return timer; },
    clearTimeout: (timer) => { timer.cancelled = true; },
    setInterval: () => ({ interval: true }),
    clearInterval: () => {},
    runDue() {
      for (const timer of timers.splice(0)) {
        if (timer.cancelled) continue;
        if (timer.at <= time) timer.callback();
        else timers.push(timer);
      }
    },
  };
}

function api(name) {
  return { name, released: 0, async release() { this.released += 1; } };
}

function storage(c, options = {}) {
  return new IdleOrchestratorStorage({ idleTtlMs: 1000, releaseGraceMs: 100, maxEntries: 2, ...c, ...options });
}

const settle = () => new Promise(setImmediate);

test('an orchestrator idle past its TTL is dropped and released after the grace period', async () => {
  const c = clock();
  const s = storage(c);
  const a = api('a');
  s.set('a', a);
  c.advance(999);
  assert.equal(s.get('a'), a);
  c.advance(999);
  assert.equal(s.has('a'), true, 'a read refreshes idleness');
  c.advance(1000);
  assert.equal(s.has('a'), false);
  c.advance(99);
  c.runDue();
  await settle();
  assert.equal(a.released, 0, 'in-flight queries get the grace period');
  c.advance(1);
  c.runDue();
  await settle();
  assert.equal(a.released, 1);
});

test('the sweeper retires idle orchestrators nobody asks for again', async () => {
  const c = clock();
  const s = storage(c);
  const a = api('a');
  const b = api('b');
  s.set('a', a);
  c.advance(600);
  s.set('b', b);
  c.advance(600);
  s.sweep();
  assert.deepEqual(s.values(), [b]);
  c.advance(100);
  c.runDue();
  await settle();
  assert.equal(a.released, 1);
  assert.equal(b.released, 0);
});

test('size eviction releases the least recently used driver instead of leaking it', async () => {
  const c = clock();
  const s = storage(c);
  const [a, b, d] = [api('a'), api('b'), api('d')];
  s.set('a', a);
  s.set('b', b);
  s.get('a');
  s.set('d', d);
  assert.equal(s.has('b'), false);
  assert.deepEqual(s.values(), [a, d]);
  c.advance(100);
  c.runDue();
  await settle();
  assert.deepEqual([a.released, b.released, d.released], [0, 1, 0]);
});

test('replacing an orchestrator retires the old one once', async () => {
  const c = clock();
  const s = storage(c);
  const [old, fresh] = [api('old'), api('fresh')];
  s.set('a', old);
  s.set('a', fresh);
  s.set('a', fresh);
  c.advance(100);
  c.runDue();
  await settle();
  assert.deepEqual([old.released, fresh.released], [1, 0]);
});

test('releaseConnections releases live and retiring orchestrators exactly once', async () => {
  const c = clock();
  const s = storage(c);
  const [a, b] = [api('a'), api('b')];
  s.set('a', a);
  s.set('b', b);
  c.advance(1000);
  assert.equal(s.has('a'), false);
  await s.releaseConnections();
  c.advance(100);
  c.runDue();
  await settle();
  assert.deepEqual([a.released, b.released], [1, 1]);
  assert.deepEqual(s.values(), []);
});

test('a failed release is logged, not thrown', async () => {
  const c = clock();
  const warnings = [];
  const s = storage(c, { logger: { warn: (...args) => warnings.push(args) } });
  s.set('a', { async release() { throw new Error('synthetic'); } });
  c.advance(1000);
  s.sweep();
  c.advance(100);
  c.runDue();
  await settle();
  assert.equal(warnings.length, 1);
  assert.equal(warnings[0][0].orchestratorId, 'a');
});

test('defaults keep upstream capacity and expire orchestrators idle for ten minutes', () => {
  const s = new IdleOrchestratorStorage();
  assert.equal(s.maxEntries, 100);
  assert.equal(s.idleTtlMs, 600000);
  assert.equal(s.releaseGraceMs, 60000);
  const upstream = new IdleOrchestratorStorage({ compilerCacheSize: 7, maxCompilerCacheKeepAlive: 1234 });
  assert.equal(upstream.maxEntries, 7);
  assert.equal(upstream.idleTtlMs, 1234);
});

function write(path, value) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, value);
}

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'scout-orchestrator-patch-'));
  t.after(() => { rmSync(root, { recursive: true, force: true }); });
  const packageRoot = join(root, 'node_modules/@cubejs-backend/server-core');
  const packagePath = join(packageRoot, 'package.json');
  const target = join(packageRoot, 'dist/src/core/OrchestratorStorage.js');
  const installedModule = join(dirname(target), 'ScoutIdleOrchestratorStorage.js');
  write(packagePath, JSON.stringify({ version: '1.6.39', main: 'index.js' }));
  write(target, ORIGINAL_SOURCE);
  write(join(root, 'node_modules/@cubejs-backend/server/bin/server'), '// fixture resolver only');
  mkdirSync(join(root, 'node_modules/.bin'), { recursive: true });
  symlinkSync('../@cubejs-backend/server/bin/server', join(root, 'node_modules/.bin/cubejs-server'));
  return { root, packagePath, target, installedModule };
}

test('upstream fixture matches the exact installed-artifact integrity guard', () => {
  assert.equal(sha256(ORIGINAL_SOURCE), ORIGINAL_SHA256);
});

test('the replacement provides every method the upstream storage does', () => {
  const upstream = [...ORIGINAL_SOURCE.matchAll(/^    (?:async )?(\w+)\(/gm)].map((m) => m[1]).filter((name) => name !== 'constructor');
  assert.deepEqual([...upstream].sort(), [...UPSTREAM_METHODS].sort());
  for (const method of upstream) {
    assert.equal(typeof IdleOrchestratorStorage.prototype[method], 'function', method);
  }
});

test('patch installs and verifies through the actual serving resolver', t => {
  const f = fixture(t);
  const result = patchOrchestratorStorage({ serverRoot: f.root });
  assert.equal(result.version, '1.6.39');
  assert.equal(readFileSync(f.target, 'utf8'), PATCHED_SOURCE);
  assert.equal(result.moduleHash, sha256(readFileSync(join(__dirname, 'idle-orchestrator-storage.js'))));
  assert.deepEqual(patchOrchestratorStorage({ serverRoot: f.root, verifyOnly: true }), result);
  assert.deepEqual(patchOrchestratorStorage({ serverRoot: f.root }), result);
});

test('version drift fails before installing anything', t => {
  const f = fixture(t);
  writeFileSync(f.packagePath, JSON.stringify({ version: '1.6.40', main: 'index.js' }));
  assert.throws(() => patchOrchestratorStorage({ serverRoot: f.root }), /before upgrading/);
  assert.equal(readFileSync(f.target, 'utf8'), ORIGINAL_SOURCE);
  assert.equal(existsSync(f.installedModule), false);
});

test('content drift in the pinned target fails closed', t => {
  const f = fixture(t);
  writeFileSync(f.target, ORIGINAL_SOURCE + '\n');
  assert.throws(() => patchOrchestratorStorage({ serverRoot: f.root }), /content drifted/);
  assert.equal(existsSync(f.installedModule), false);
});

test('startup verification neither installs a missing patch nor accepts a modified one', t => {
  const f = fixture(t);
  assert.throws(() => patchOrchestratorStorage({ serverRoot: f.root, verifyOnly: true }), /patch is missing/);
  assert.equal(readFileSync(f.target, 'utf8'), ORIGINAL_SOURCE);
  patchOrchestratorStorage({ serverRoot: f.root });
  writeFileSync(f.installedModule, readFileSync(f.installedModule, 'utf8') + '\n');
  assert.throws(() => patchOrchestratorStorage({ serverRoot: f.root, verifyOnly: true }), /module content drifted/);
});

test('image build and both startup paths validate the orchestrator patch', () => {
  const dockerfile = readFileSync(join(__dirname, 'Dockerfile'), 'utf8');
  assert.match(dockerfile, /^COPY pinned-patch\.js \/cube\/conf\/pinned-patch\.js$/m);
  assert.match(dockerfile, /^COPY idle-orchestrator-storage\.js \/cube\/conf\/idle-orchestrator-storage\.js$/m);
  assert.match(dockerfile, /^COPY patch-orchestrator-storage\.js \/cube\/conf\/patch-orchestrator-storage\.js$/m);
  assert.match(dockerfile, /&& node \/cube\/conf\/patch-orchestrator-storage\.js \\/);
  assert.match(dockerfile, /CMD .*patch-orchestrator-storage\.js --verify && /);
  const packageJson = JSON.parse(readFileSync(join(__dirname, 'package.json'), 'utf8'));
  assert.match(packageJson.scripts.start, /^node \/cube\/conf\/patch-orchestrator-storage\.js --verify && /);
});
