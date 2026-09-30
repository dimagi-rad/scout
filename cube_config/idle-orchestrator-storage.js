'use strict';

// Cube 1.6.39 keeps every orchestrator (and its driver pool) in an LRU of 100
// with no idle expiry, and drops evicted ones without releasing their drivers.
const DEFAULTS = Object.freeze({
  maxEntries: 100,
  // Use is refreshed on every request, including each "Continue wait" poll, and
  // Cube's queue drops a job nobody has polled for 120s (orphanedTimeout). So no
  // work can outlive 10 idle minutes.
  idleTtlMs: 10 * 60 * 1000,
  // Covers the pool-size eviction path, where a query admitted just before can
  // still be waiting for a connection (20s) or running (30s statement timeout).
  releaseGraceMs: 60 * 1000,
  sweepIntervalMs: 60 * 1000,
});

class IdleOrchestratorStorage {
  // Also accepts upstream's option names; Cube 1.6.39 itself passes none.
  constructor({
    compilerCacheSize,
    maxCompilerCacheKeepAlive,
    maxEntries = compilerCacheSize || DEFAULTS.maxEntries,
    idleTtlMs = maxCompilerCacheKeepAlive || DEFAULTS.idleTtlMs,
    releaseGraceMs = DEFAULTS.releaseGraceMs,
    sweepIntervalMs = DEFAULTS.sweepIntervalMs,
    now = Date.now,
    setTimeout: schedule = globalThis.setTimeout,
    clearTimeout: cancel = globalThis.clearTimeout,
    setInterval: repeat = globalThis.setInterval,
    clearInterval: stopRepeat = globalThis.clearInterval,
    logger = console,
  } = {}) {
    this.maxEntries = maxEntries;
    this.idleTtlMs = idleTtlMs;
    this.releaseGraceMs = releaseGraceMs;
    this.sweepIntervalMs = sweepIntervalMs;
    this.now = now;
    this.schedule = schedule;
    this.cancel = cancel;
    this.repeat = repeat;
    this.stopRepeat = stopRepeat;
    this.logger = logger;
    this.entries = new Map();
    this.retiring = new Map();
    this.sweeper = null;
  }

  isIdle(entry) {
    return this.now() - entry.lastUsed >= this.idleTtlMs;
  }

  has(orchestratorId) {
    const entry = this.entries.get(orchestratorId);
    if (!entry) {
      return false;
    }
    if (this.isIdle(entry)) {
      this.retire(orchestratorId, entry);
      return false;
    }
    return true;
  }

  get(orchestratorId) {
    if (!this.has(orchestratorId)) {
      return undefined;
    }
    const entry = this.entries.get(orchestratorId);
    entry.lastUsed = this.now();
    this.entries.delete(orchestratorId);
    this.entries.set(orchestratorId, entry);
    return entry.api;
  }

  set(orchestratorId, orchestratorApi) {
    const previous = this.entries.get(orchestratorId);
    if (previous && previous.api !== orchestratorApi) {
      this.retire(orchestratorId, previous);
    }
    this.entries.delete(orchestratorId);
    this.entries.set(orchestratorId, { api: orchestratorApi, lastUsed: this.now() });
    while (this.entries.size > this.maxEntries) {
      const [oldestId, oldest] = this.entries.entries().next().value;
      this.retire(oldestId, oldest);
    }
    this.ensureSweeper();
    return this;
  }

  retire(orchestratorId, entry) {
    this.entries.delete(orchestratorId);
    if (this.retiring.has(entry.api)) {
      return;
    }
    const timer = this.schedule(() => {
      this.retiring.delete(entry.api);
      this.release(orchestratorId, entry.api);
    }, this.releaseGraceMs);
    timer?.unref?.();
    this.retiring.set(entry.api, timer);
    this.stopSweeperWhenEmpty();
  }

  release(orchestratorId, orchestratorApi) {
    Promise.resolve()
      .then(() => orchestratorApi.release())
      .catch((error) => {
        this.logger.warn({ orchestratorId, error: error?.message }, 'Releasing an idle Cube orchestrator failed');
      });
  }

  sweep() {
    for (const [orchestratorId, entry] of this.entries) {
      if (this.isIdle(entry)) {
        this.retire(orchestratorId, entry);
      }
    }
  }

  ensureSweeper() {
    if (this.sweeper) {
      return;
    }
    this.sweeper = this.repeat(() => this.sweep(), this.sweepIntervalMs);
    this.sweeper?.unref?.();
  }

  stopSweeperWhenEmpty() {
    if (this.sweeper && this.entries.size === 0) {
      this.stopRepeat(this.sweeper);
      this.sweeper = null;
    }
  }

  // Upstream clear() forgets orchestrators without releasing them; its callers
  // release first via releaseConnections().
  clear() {
    this.entries.clear();
    this.stopSweeperWhenEmpty();
  }

  values() {
    return [...this.entries.values()].map((entry) => entry.api);
  }

  async testConnections() {
    return Promise.all(this.values().map((api) => api.testConnection()));
  }

  async testOrchestratorConnections() {
    return Promise.all(this.values().map((api) => api.testOrchestratorConnections()));
  }

  async releaseConnections() {
    const apis = this.values();
    for (const [api, timer] of this.retiring) {
      this.cancel(timer);
      apis.push(api);
    }
    this.retiring.clear();
    this.clear();
    await Promise.all(apis.map((api) => api.release()));
  }
}

module.exports = { DEFAULTS, IdleOrchestratorStorage };
