'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { BoundedLocalCacheStore, createLocalCacheDriver, DEFAULT_LIMITS, getSharedStoreStats, limitsFromEnv, RESULT_TOO_LARGE_CODE, RESULT_TOO_LARGE_MESSAGE } = require('./bounded-local-cache');

const key = suffix => `workspace#SQL_QUERY_RESULT:${suffix}`;

// The driver must delegate cancellation to Cube's supplied wrapper/token.
function cancellable(fn) {
  const callbacks = [];
  let cancelled = false;
  const promise = fn({ with: async pending => {
    if (pending.cancel) callbacks.push(pending.cancel);
    return pending;
  } });
  promise.cancel = async (waitExecution = true) => {
    assert.equal(cancelled, false);
    cancelled = true;
    await Promise.all([...callbacks.map(cancel => cancel()), ...(waitExecution ? [promise] : [])]);
  };
  return promise;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function fixture(options = {}) {
  let time = 1000;
  let created = 0;
  let unref = 0;
  const timers = new Set();
  const store = new BoundedLocalCacheStore({
    maxResultEntries: 2, maxResultBytes: 1024, maxLocks: 2, sweepIntervalMs: 10,
    now: () => time,
    setInterval: callback => {
      const timer = { callback, unref: () => { unref += 1; } };
      timers.add(timer);
      created += 1;
      return timer;
    },
    clearInterval: timer => { timers.delete(timer); },
    ...options,
  });
  const Driver = createLocalCacheDriver({ createCancelablePromise: cancellable, store });
  return {
    store, Driver, timers,
    advance: milliseconds => { time += milliseconds; },
    tick: () => { for (const timer of [...timers]) timer.callback(); },
    timerCounts: () => ({ created, unref, live: timers.size }),
  };
}

test('shared stats do not initialize a store or read configuration', () => {
  const previous = process.env.SCOUT_CUBE_CACHE_MAX_RESULT_BYTES;
  process.env.SCOUT_CUBE_CACHE_MAX_RESULT_BYTES = 'invalid';
  try {
    assert.deepEqual(getSharedStoreStats(), {
      resultEntries: 0, resultBytes: 0, metadataEntries: 0, lockEntries: 0, timerActive: false,
    });
  } finally {
    if (previous === undefined) delete process.env.SCOUT_CUBE_CACHE_MAX_RESULT_BYTES;
    else process.env.SCOUT_CUBE_CACHE_MAX_RESULT_BYTES = previous;
  }
});

test('result LRU is bounded by entries and touches successful reads', () => {
  const { store } = fixture();
  store.set(key('a'), 1, 60);
  store.set(key('b'), 2, 60);
  assert.equal(store.get(key('a')), 1);
  store.set(key('c'), 3, 60);
  assert.equal(store.get(key('b')), undefined);
  assert.equal(store.get(key('a')), 1);
  assert.equal(store.getStats().resultEntries, 2);
});

test('serialized payload and UTF-8 key bytes bound retention', () => {
  const name = key('é');
  const value = '🌊'.repeat(250);
  const bytes = Buffer.byteLength(name) + Buffer.byteLength(JSON.stringify(value));
  const { store } = fixture({ maxResultBytes: bytes });
  assert.equal(store.set(name, value, 60).bytes, Buffer.byteLength(JSON.stringify(value)));
  assert.equal(store.getStats().resultBytes, bytes);
  assert.equal(store.get(name), value);
});

test('byte pressure evicts oldest results even below entry cap', () => {
  const { store } = fixture({ maxResultEntries: 100, maxResultBytes: 1024 });
  store.set(key('a'), 'a'.repeat(400), 60);
  store.set(key('b'), 'b'.repeat(400), 60);
  store.set(key('c'), 'c'.repeat(400), 60);
  assert.equal(store.getStats().resultEntries, 2);
  assert(store.getStats().resultBytes <= 1024);
  assert.equal(store.get(key('a')), undefined);
});

test('overwrite and remove account bytes exactly without accumulating timers', () => {
  const { store, timerCounts } = fixture();
  store.set(key('a'), 'before', 60);
  store.set(key('a'), 'after', 60);
  assert.equal(store.getStats().resultEntries, 1);
  assert.equal(store.getStats().resultBytes, Buffer.byteLength(key('a')) + 7);
  assert.deepEqual(timerCounts(), { created: 1, unref: 1, live: 1 });
  store.remove(key('a'));
  store.remove(key('a'));
  assert.equal(store.getStats().resultBytes, 0);
  assert.equal(timerCounts().live, 0);
});

test('oversized replacements resolve with a compact terminal marker, never stale rows', async () => {
  const { Driver } = fixture();
  const driver = new Driver();
  await driver.set(key('a'), ['old'], 60);
  const payload = ['new'.repeat(1000)];
  const result = await driver.set(key('a'), payload, 60);
  const markerBytes = Buffer.byteLength(JSON.stringify({ error: RESULT_TOO_LARGE_CODE }));
  assert.equal(result.bytes, markerBytes);
  await assert.rejects(driver.get(key('a')), { code: RESULT_TOO_LARGE_CODE, message: RESULT_TOO_LARGE_MESSAGE });
  assert.equal(driver.getStats().resultEntries, 1);
  assert.equal(driver.getStats().resultBytes, Buffer.byteLength(key('a')) + markerBytes);
});

test('terminal markers expire after at most 30 seconds and polls never extend them', () => {
  const { store, advance, tick } = fixture();
  store.set(key('a'), 'x'.repeat(2048), 120);
  advance(29999);
  assert.throws(() => store.get(key('a')), { code: RESULT_TOO_LARGE_CODE });
  advance(1);
  assert.equal(store.getStats().resultEntries, 1);
  tick();
  assert.equal(store.getStats().resultEntries, 0);
  assert.equal(store.getStats().resultBytes, 0);
  assert.equal(store.getStats().timerActive, false);
});

test('terminal marker expiry respects a shorter original TTL', () => {
  const { store, advance, tick } = fixture();
  store.set(key('a'), 'x'.repeat(2048), 1);
  advance(1000);
  tick();
  assert.equal(store.getStats().resultEntries, 0);
});

test('marker pressure, successful replacement, and reset use the same byte accounting', () => {
  const { store } = fixture({ maxResultEntries: 1 });
  store.set(key('a'), 'x'.repeat(2048), 60);
  store.set(key('b'), 'x'.repeat(2048), 60);
  assert.equal(store.get(key('a')), undefined);
  assert.equal(store.getStats().resultEntries, 1);
  store.set(key('b'), { rows: [1] }, 60);
  assert.deepEqual(store.get(key('b')), { rows: [1] });
  assert.equal(store.getStats().resultBytes,
    Buffer.byteLength(key('b')) + Buffer.byteLength(JSON.stringify({ rows: [1] })));
  store.set(key('b'), 'x'.repeat(2048), 60);
  store.reset();
  assert.equal(store.getStats().resultBytes, 0);
});

test('markers cannot be mutated through repeated error objects', () => {
  const { store } = fixture();
  store.set(key('a'), 'x'.repeat(2048), 60);
  try { store.get(key('a')); } catch (error) { error.message = 'changed'; error.code = 'changed'; }
  assert.throws(() => store.get(key('a')), { code: RESULT_TOO_LARGE_CODE, message: RESULT_TOO_LARGE_MESSAGE });
});

test('maximum UTF-8 result key always admits a marker; longer keys fail before any cache work', () => {
  const { store } = fixture();
  const prefix = key('');
  const remaining = 512 - Buffer.byteLength(prefix);
  const maximum = prefix + 'é'.repeat(Math.floor(remaining / 2)) + 'a'.repeat(remaining % 2);
  assert.equal(Buffer.byteLength(maximum), 512);
  store.set(maximum, 'x'.repeat(2048), 60);
  assert(store.getStats().resultBytes <= 1024);
  assert.throws(() => store.get(maximum), { code: RESULT_TOO_LARGE_CODE });
  assert.throws(() => store.get(maximum + 'a'), /exceeds 512 bytes/);
  assert.throws(() => store.set(maximum + 'a', 1, 60), /exceeds 512 bytes/);
  assert.equal(store.getStats().resultEntries, 1);
});

test('mutating caller results after set cannot bypass serialized-byte accounting', () => {
  const { store } = fixture();
  const value = { rows: [1] };
  store.set(key('a'), value, 60);
  const bytes = store.getStats().resultBytes;
  value.rows.push('large'.repeat(1000));
  assert.deepEqual(store.get(key('a')), { rows: [1] });
  assert.equal(store.getStats().resultBytes, bytes);
});

test('get returns independent JSON results while metadata identity stays unchanged', () => {
  const { store } = fixture();
  store.set(key('a'), { rows: [1] }, 60);
  const returned = store.get(key('a'));
  returned.rows.push('large'.repeat(1000));
  assert.deepEqual(store.get(key('a')), { rows: [1] });
  const metadata = { used: true };
  store.set('SQL_PRE_AGGREGATIONS_TABLES_USED_workspace', metadata, 60);
  assert.equal(store.get('SQL_PRE_AGGREGATIONS_TABLES_USED_workspace'), metadata);
});

test('one unref timer actively expires unread results; stats do not sweep', () => {
  const { store, advance, tick, timerCounts } = fixture();
  store.set(key('never-read'), [{ count: 4 }], 1);
  advance(1000);
  assert.equal(store.getStats().resultEntries, 1);
  tick();
  assert.equal(store.getStats().resultEntries, 0);
  assert.equal(store.getStats().resultBytes, 0);
  assert.deepEqual(timerCounts(), { created: 1, unref: 1, live: 0 });
});

test('expiry boundary and zero TTL never return an expired replacement', () => {
  const { store, advance } = fixture();
  store.set(key('a'), 1, 0.01);
  advance(9);
  assert.equal(store.get(key('a')), 1);
  advance(1);
  assert.equal(store.get(key('a')), undefined);
  store.set(key('a'), 2, 10);
  store.set(key('a'), 3, 0);
  assert.equal(store.get(key('a')), undefined);
  assert.equal(store.getStats().timerActive, false);
});

test('shared driver instances preserve live values and locks on cleanup', async () => {
  const { Driver, advance } = fixture();
  const first = new Driver();
  const second = new Driver();
  await first.set(key('a'), { count: 1 }, 60);
  assert.equal(await first.withLock('lock:tenant', async () => {}, 60, false), true);
  advance(1);
  await second.cleanup();
  assert.deepEqual(await second.get(key('a')), { count: 1 });
  assert.equal(await second.withLock('lock:tenant', async () => {}), false);
  assert.equal(second.getStats().timerActive, true);
});

test('reset is global across instances and future admissions restart one timer', async () => {
  const { Driver, timerCounts } = fixture();
  const first = new Driver();
  const second = new Driver();
  await first.set(key('a'), 1, 60);
  await first.withLock('lock:a', async () => {}, 60, false);
  second.reset();
  assert.equal(await first.get(key('a')), undefined);
  assert.deepEqual(first.getStats(), {
    resultEntries: 0, resultBytes: 0, metadataEntries: 0, lockEntries: 0, timerActive: false,
  });
  await second.set(key('b'), 2, 60);
  assert.equal(await first.get(key('b')), 2);
  assert.deepEqual(timerCounts(), { created: 2, unref: 2, live: 1 });
});

test('a cancelled old sweep cannot delete entries admitted after reset', () => {
  const { store, timers, advance, tick } = fixture();
  store.set(key('a'), 1, 1);
  const oldTimer = [...timers][0];
  store.reset();
  store.set(key('a'), 2, 1);
  advance(1000);
  oldTimer.callback();
  assert.equal(store.getStats().resultEntries, 1);
  tick();
  assert.equal(store.getStats().resultEntries, 0);
});

test('only exact query-result namespace uses LRU; safety metadata retains its TTL', () => {
  const { store, advance, tick } = fixture({ maxResultEntries: 1 });
  const marker = 'SQL_PRE_AGGREGATIONS_TABLES_USED_workspace';
  const other = 'workspace#OTHER:literal#SQL_QUERY_RESULT:label';
  store.set(marker, 'required'.repeat(100), 2);
  store.set(other, true, 2);
  for (let i = 0; i < 100; i += 1) store.set(key(i), i, 60);
  assert.equal(store.get(marker), 'required'.repeat(100));
  assert.equal(store.get(other), true);
  assert.equal(store.getStats().metadataEntries, 2);
  advance(2000);
  tick();
  assert.equal(store.getStats().metadataEntries, 0);
  assert.equal(store.getStats().resultEntries, 1);
});

test('result pressure cannot evict an active lock or a retained lease', async () => {
  const { Driver } = fixture({ maxResultEntries: 1 });
  const driver = new Driver();
  const held = deferred();
  const active = driver.withLock('lock:active', () => held.promise);
  await driver.withLock('lock:retained', async () => {}, 60, false);
  for (let i = 0; i < 100; i += 1) await driver.set(key(i), i, 60);
  assert.equal(await driver.withLock('lock:active', async () => {}), false);
  assert.equal(await driver.withLock('lock:retained', async () => {}), false);
  assert.equal(driver.getStats().lockEntries, 2);
  held.resolve();
  assert.equal(await active, true);
  assert.equal(driver.getStats().lockEntries, 1);
});

test('lock capacity fails closed without executing or evicting active work', async () => {
  const { Driver, advance, tick } = fixture({ maxLocks: 1 });
  const driver = new Driver();
  await driver.withLock('lock:a', async () => {}, 1, false);
  let calls = 0;
  assert.equal(await driver.withLock('lock:b', async () => { calls += 1; }), false);
  assert.equal(calls, 0);
  assert.equal(driver.getStats().lockEntries, 1);
  advance(1000);
  tick();
  assert.equal(driver.getStats().lockEntries, 0);
  assert.equal(await driver.withLock('lock:b', async () => { calls += 1; }), true);
  assert.equal(calls, 1);
});

test('expired owner cannot release the successor lease', async () => {
  const { Driver, advance } = fixture();
  const driver = new Driver();
  const oldWork = deferred();
  const newWork = deferred();
  const oldOwner = driver.withLock('lock:a', () => oldWork.promise, 1);
  advance(1000);
  const newOwner = driver.withLock('lock:a', () => newWork.promise, 60);
  oldWork.resolve();
  assert.equal(await oldOwner, true);
  assert.equal(await driver.withLock('lock:a', async () => {}), false);
  newWork.resolve();
  assert.equal(await newOwner, true);
  assert.equal(driver.getStats().lockEntries, 0);
});

test('lock callback failure frees ordinary leases but preserves freeAfter=false', async () => {
  const { Driver } = fixture();
  const driver = new Driver();
  for (const freeAfter of [true, false]) {
    await assert.rejects(driver.withLock('lock:a', async () => { throw new Error('synthetic'); }, 60, freeAfter), /synthetic/);
    assert.equal(driver.getStats().lockEntries, freeAfter ? 0 : 1);
  }
});

test('cancellation uses the provided token and drains work before releasing', async () => {
  const { Driver } = fixture();
  const driver = new Driver();
  const held = deferred();
  let cancelled = false;
  held.promise.cancel = async () => { cancelled = true; held.resolve(); };
  const lock = driver.withLock('lock:a', () => held.promise);
  assert.equal(driver.getStats().lockEntries, 1);
  await lock.cancel();
  assert.equal(cancelled, true);
  assert.equal(await lock, true);
  assert.equal(driver.getStats().lockEntries, 0);
});

test('cancelled freeAfter=false work leaves its lease until normal expiry', async () => {
  const { Driver, advance, tick } = fixture();
  const driver = new Driver();
  const held = deferred();
  held.promise.cancel = async () => { held.resolve(); };
  const lock = driver.withLock('lock:a', () => held.promise, 1, false);
  await lock.cancel();
  assert.equal(driver.getStats().lockEntries, 1);
  advance(1000);
  tick();
  assert.equal(driver.getStats().lockEntries, 0);
});

test('keys and remove preserve shared metadata/lock visibility without leaking expired values', async () => {
  const { Driver, advance } = fixture();
  const driver = new Driver();
  await driver.set('shared:metadata', 0, 1);
  await driver.withLock('shared:lock', async () => {}, 60, false);
  assert.deepEqual((await driver.keysStartingWith('shared:')).sort(), ['shared:lock', 'shared:metadata']);
  assert.equal(await driver.get('shared:metadata'), 0);
  advance(1000);
  assert.deepEqual(await driver.keysStartingWith('shared:'), ['shared:lock']);
  await driver.remove('shared:lock');
  assert.equal(driver.getStats().timerActive, false);
});

test('production bounds validate environment and timer range without silent coercion', () => {
  assert.deepEqual(limitsFromEnv({}), DEFAULT_LIMITS);
  assert.equal(limitsFromEnv({ SCOUT_CUBE_CACHE_SWEEP_INTERVAL_MS: '20' }).sweepIntervalMs, 20);
  for (const bad of ['', '0', '-1', '1.5', 'Infinity', '9007199254740992', ' 2', '02']) {
    assert.throws(() => limitsFromEnv({ SCOUT_CUBE_CACHE_MAX_RESULT_BYTES: bad }), /positive safe integer/);
  }
  assert.throws(() => limitsFromEnv({ SCOUT_CUBE_CACHE_SWEEP_INTERVAL_MS: '2147483648' }), /timer range/);
  assert.throws(() => limitsFromEnv({ SCOUT_CUBE_CACHE_MAX_RESULT_BYTES: '1023' }), /at least 1024/);
  assert.throws(() => fixture({ maxResultBytes: 1023 }), /at least 1024/);
  for (const bad of [0, -1, NaN, Infinity, 1.5]) {
    assert.throws(() => fixture({ maxLocks: bad }), /positive safe integer/);
  }
});

test('invalid TTLs fail before admission and stats expose no values or keys', () => {
  const { store } = fixture();
  for (const ttl of [-1, NaN, Infinity, '10']) {
    assert.throws(() => store.set(key('private'), { private: true }, ttl), /expiration/);
  }
  assert.deepEqual(store.getStats(), {
    resultEntries: 0, resultBytes: 0, metadataEntries: 0, lockEntries: 0, timerActive: false,
  });
});
