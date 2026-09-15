'use strict';

const DEFAULT_LIMITS = Object.freeze({
  maxResultEntries: 4096,
  maxResultBytes: 64 * 1024 * 1024,
  maxLocks: 1024,
  sweepIntervalMs: 15000,
});

const RESULT_TOO_LARGE_CODE = 'SCOUT_CUBE_RESULT_TOO_LARGE';
const RESULT_TOO_LARGE_MESSAGE = 'Query result exceeds the configured in-memory cache limit. Reduce the number of rows or columns and try again.';
const ERROR_MARKER = JSON.stringify({ error: RESULT_TOO_LARGE_CODE });
const MAX_RESULT_KEY_BYTES = 512;

function isResultKey(key) {
  return key.slice(key.indexOf('#')).startsWith('#SQL_QUERY_RESULT:');
}

function checkResultKey(key) {
  if (isResultKey(key) && Buffer.byteLength(key) > MAX_RESULT_KEY_BYTES) {
    throw new Error('Cube query-result cache key exceeds 512 bytes; review the cache namespace configuration');
  }
}

const ENV_LIMITS = Object.freeze({
  maxResultEntries: 'SCOUT_CUBE_CACHE_MAX_RESULT_ENTRIES',
  maxResultBytes: 'SCOUT_CUBE_CACHE_MAX_RESULT_BYTES',
  maxLocks: 'SCOUT_CUBE_CACHE_MAX_LOCKS',
  sweepIntervalMs: 'SCOUT_CUBE_CACHE_SWEEP_INTERVAL_MS',
});

function positiveInteger(value, name) {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`${name} must be a positive safe integer`);
  }
  if (name === 'sweepIntervalMs' && value > 2147483647) {
    throw new Error('sweepIntervalMs exceeds the Node timer range');
  }
  if (name === 'maxResultBytes' && value < 1024) {
    throw new Error('maxResultBytes must be at least 1024 bytes');
  }
  return value;
}

function limitsFromEnv(env) {
  return Object.fromEntries(Object.entries(ENV_LIMITS).map(([name, key]) => {
    const raw = env[key];
    if (raw !== undefined && !/^[1-9][0-9]*$/.test(raw)) {
      throw new Error(`${key} must be a positive safe integer`);
    }
    return [name, positiveInteger(raw === undefined ? DEFAULT_LIMITS[name] : Number(raw), name)];
  }));
}

class BoundedLocalCacheStore {
  constructor({
    maxResultEntries = DEFAULT_LIMITS.maxResultEntries,
    maxResultBytes = DEFAULT_LIMITS.maxResultBytes,
    maxLocks = DEFAULT_LIMITS.maxLocks,
    sweepIntervalMs = DEFAULT_LIMITS.sweepIntervalMs,
    now = Date.now,
    setInterval: schedule = globalThis.setInterval,
    clearInterval: unschedule = globalThis.clearInterval,
  } = {}) {
    this.maxResultEntries = positiveInteger(maxResultEntries, 'maxResultEntries');
    this.maxResultBytes = positiveInteger(maxResultBytes, 'maxResultBytes');
    this.maxLocks = positiveInteger(maxLocks, 'maxLocks');
    this.sweepIntervalMs = positiveInteger(sweepIntervalMs, 'sweepIntervalMs');
    this.now = now;
    this.schedule = schedule;
    this.unschedule = unschedule;
    this.results = new Map();
    this.metadata = new Map();
    this.locks = new Map();
    this.resultBytes = 0;
    this.timer = null;
    this.timerGeneration = 0;
  }

  expiresAt(expiration) {
    if (typeof expiration !== 'number' || !Number.isFinite(expiration) || expiration < 0) {
      throw new Error('Cache expiration must be finite non-negative seconds');
    }
    const at = this.now() + expiration * 1000;
    if (!Number.isFinite(at) || at > Number.MAX_SAFE_INTEGER) {
      throw new Error('Cache expiration exceeds the supported timestamp range');
    }
    return at;
  }

  ensureTimer() {
    if (this.timer || !this.hasEntries()) return;
    const generation = ++this.timerGeneration;
    this.timer = this.schedule(() => {
      if (generation === this.timerGeneration) this.sweep();
    }, this.sweepIntervalMs);
    this.timer.unref();
  }

  hasEntries() {
    return this.results.size + this.metadata.size + this.locks.size > 0;
  }

  stopTimerWhenEmpty() {
    if (this.timer && !this.hasEntries()) {
      this.unschedule(this.timer);
      this.timer = null;
      this.timerGeneration += 1;
    }
  }

  removeResult(key) {
    const entry = this.results.get(key);
    if (entry) {
      this.resultBytes -= entry.bytes;
      this.results.delete(key);
    }
  }

  expireKey(key) {
    const now = this.now();
    if (this.results.get(key)?.expiresAt <= now) this.removeResult(key);
    if (this.metadata.get(key)?.expiresAt <= now) this.metadata.delete(key);
    if (this.locks.get(key)?.expiresAt <= now) this.locks.delete(key);
  }

  get(key) {
    checkResultKey(key);
    this.expireKey(key);
    const result = this.results.get(key);
    if (result) {
      this.results.delete(key);
      this.results.set(key, result);
      if (result.error) {
        const error = new Error(RESULT_TOO_LARGE_MESSAGE);
        error.code = RESULT_TOO_LARGE_CODE;
        throw error;
      }
      return JSON.parse(result.serialized);
    }
    const entry = this.metadata.get(key) || this.locks.get(key);
    this.stopTimerWhenEmpty();
    return entry?.value;
  }

  set(key, value, expiration) {
    checkResultKey(key);
    let expiresAt = this.expiresAt(expiration);
    let serialized = JSON.stringify(value);
    const payloadBytes = Buffer.byteLength(serialized);
    const keyBytes = Buffer.byteLength(key);
    let bytes = payloadBytes + keyBytes;
    this.sweep();
    // A rejected oversized replacement must not resurrect the previous value.
    this.removeResult(key);
    this.metadata.delete(key);
    this.locks.delete(key);
    const isResult = isResultKey(key);
    if (expiresAt <= this.now()) {
      this.stopTimerWhenEmpty();
      return { key, bytes: payloadBytes };
    }
    const error = isResult && bytes > this.maxResultBytes;
    if (error) {
      // Dropping oversized results makes already-timed-out Cube requests loop.
      // Keep a bounded terminal outcome so their next poll fails clearly.
      serialized = ERROR_MARKER;
      bytes = Buffer.byteLength(serialized) + keyBytes;
      expiresAt = Math.min(expiresAt, this.now() + 30000);
    }
    if (isResult) {
      while (this.results.size >= this.maxResultEntries || this.resultBytes + bytes > this.maxResultBytes) {
        this.removeResult(this.results.keys().next().value);
      }
      // Result payloads follow CubeStore's JSON contract. Caller mutations must
      // not grow a retained object behind the serialized-byte accounting.
      this.results.set(key, { serialized, bytes, expiresAt, error });
      this.resultBytes += bytes;
    } else {
      // Pre-aggregation safety markers must never be evicted by result pressure.
      this.metadata.set(key, { value, expiresAt });
    }
    this.ensureTimer();
    return { key, bytes: Buffer.byteLength(serialized) };
  }

  remove(key) {
    this.removeResult(key);
    this.metadata.delete(key);
    this.locks.delete(key);
    this.stopTimerWhenEmpty();
  }

  keysStartingWith(prefix) {
    this.sweep();
    return [...this.results.keys(), ...this.metadata.keys(), ...this.locks.keys()]
      .filter(key => key.startsWith(prefix));
  }

  sweep() {
    const now = this.now();
    for (const [key, entry] of this.results) {
      if (entry.expiresAt <= now) this.removeResult(key);
    }
    for (const [key, entry] of this.metadata) {
      if (entry.expiresAt <= now) this.metadata.delete(key);
    }
    for (const [key, entry] of this.locks) {
      if (entry.expiresAt <= now) this.locks.delete(key);
    }
    this.stopTimerWhenEmpty();
  }

  acquireLock(key, expiration) {
    const expiresAt = this.expiresAt(expiration);
    this.sweep();
    if (this.results.has(key) || this.metadata.has(key) || this.locks.has(key) ||
        this.locks.size >= this.maxLocks || expiresAt <= this.now()) return null;
    const token = Symbol('lock-lease');
    this.locks.set(key, { token, value: Math.random(), expiresAt });
    this.ensureTimer();
    return token;
  }

  releaseLock(key, token) {
    // An expired owner's finally block cannot unlock its successor's lease.
    if (this.locks.get(key)?.token === token) this.locks.delete(key);
    this.stopTimerWhenEmpty();
  }

  reset() {
    this.results.clear();
    this.metadata.clear();
    this.locks.clear();
    this.resultBytes = 0;
    this.stopTimerWhenEmpty();
  }

  getStats() {
    return {
      resultEntries: this.results.size,
      resultBytes: this.resultBytes,
      metadataEntries: this.metadata.size,
      lockEntries: this.locks.size,
      timerActive: this.timer !== null,
    };
  }
}

let sharedStore;
function getSharedStore() {
  sharedStore ??= new BoundedLocalCacheStore(limitsFromEnv(process.env));
  return sharedStore;
}

function createLocalCacheDriver({ createCancelablePromise, store = getSharedStore() }) {
  return class LocalCacheDriver {
    async get(key) { return store.get(key); }
    async set(key, value, expiration) { return store.set(key, value, expiration); }
    async remove(key) { store.remove(key); }
    async keysStartingWith(prefix) { return store.keysStartingWith(prefix); }
    async cleanup() { store.sweep(); }
    reset() { store.reset(); }
    async testConnection() { }
    getStats() { return store.getStats(); }

    withLock = (key, cb, expiration = 60, freeAfter = true) => createCancelablePromise(async (token) => {
      const lease = store.acquireLock(key, expiration);
      if (!lease) return false;
      try {
        await token.with(cb());
        return true;
      } finally {
        if (freeAfter) store.releaseLock(key, lease);
      }
    });
  };
}

module.exports = {
  BoundedLocalCacheStore,
  createLocalCacheDriver,
  DEFAULT_LIMITS,
  getSharedStoreStats: () => sharedStore?.getStats() ?? {
    resultEntries: 0,
    resultBytes: 0,
    metadataEntries: 0,
    lockEntries: 0,
    timerActive: false,
  },
  limitsFromEnv,
  RESULT_TOO_LARGE_CODE,
  RESULT_TOO_LARGE_MESSAGE,
};
