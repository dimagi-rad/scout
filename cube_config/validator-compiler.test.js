'use strict';

const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { test } = require('node:test');
const { createWorkerCompiler, defaultMaxQueueWaitMs } = require('./validator-compiler');

class FakeWorker extends EventEmitter {
  constructor() {
    super();
    this.posted = [];
    this.terminated = false;
  }

  postMessage(message) { this.posted.push(message); }

  async terminate() {
    this.terminated = true;
    this.emit('exit', 1);
  }

  reply(index, body) { this.emit('message', { id: this.posted[index].id, ...body }); }
}

function harness({ failRestart = false, ...options } = {}) {
  let time = 0;
  const timers = [];
  const workers = [];
  const exits = [];
  const compile = createWorkerCompiler({
    timeoutMs: 1000,
    maxQueueWaitMs: 5000,
    workerFactory: () => {
      if (failRestart && workers.length === 1) throw new Error('synthetic restart failure');
      const worker = new FakeWorker();
      workers.push(worker);
      return worker;
    },
    setTimeout: (callback, ms) => { const timer = { callback, at: time + ms }; timers.push(timer); return timer; },
    clearTimeout: (timer) => { timer.cleared = true; },
    exit: (code) => exits.push(code),
    ...options,
  });
  return {
    compile,
    workers,
    exits,
    advance(ms) {
      time += ms;
      for (const timer of timers.splice(0)) {
        if (timer.cleared) continue;
        if (timer.at <= time) timer.callback();
        else timers.push(timer);
      }
    },
  };
}

const settle = () => new Promise(setImmediate);
const outcome = (promise) => promise.then((value) => ({ value }), (error) => ({ error: error.message }));

test('compiles run one at a time in arrival order', async () => {
  const h = harness();
  const first = h.compile('a');
  const second = h.compile('b');
  const [worker] = h.workers;
  assert.deepEqual(worker.posted.map((m) => m.schema), ['a']);

  worker.reply(0, { result: { valid: true } });
  assert.deepEqual(await first, { valid: true });
  assert.deepEqual(worker.posted.map((m) => m.schema), ['a', 'b']);
  worker.reply(1, { error: 'bad yaml' });
  await assert.rejects(second, /bad yaml/);
});

test('the timeout starts at dispatch, not at enqueue', async () => {
  const h = harness();
  const first = h.compile('a');
  const second = outcome(h.compile('b'));
  const [worker] = h.workers;
  h.advance(900);
  worker.reply(0, { result: 'first' });
  assert.equal(await first, 'first');

  h.advance(900);
  worker.reply(1, { result: 'second' });
  assert.deepEqual(await second, { value: 'second' }, 'queued time must not count against the compile');
  assert.equal(worker.terminated, false);
});

test('a timeout rejects only the running compile and the queue continues on a fresh worker', async () => {
  const h = harness();
  const slow = outcome(h.compile('slow'));
  const next = h.compile('next');
  h.advance(1000);
  assert.deepEqual(await slow, { error: 'Cube schema validation timed out after 1000ms' });
  await settle();

  assert.equal(h.workers[0].terminated, true);
  assert.deepEqual(h.exits, [], 'an intentional restart is not a worker failure');
  const fresh = h.workers[1];
  assert.deepEqual(fresh.posted.map((m) => m.schema), ['next']);
  fresh.reply(0, { result: 'ok' });
  assert.equal(await next, 'ok');
});

test('a late reply from a timed-out compile is ignored', async () => {
  const h = harness();
  const slow = outcome(h.compile('slow'));
  const [old] = h.workers;
  h.advance(1000);
  await slow;
  old.reply(0, { result: 'too late' });
  await settle();
  const next = h.compile('next');
  h.workers[1].reply(0, { result: 'fresh' });
  assert.equal(await next, 'fresh');
});

test('a queued request is rejected promptly at its queue deadline, without compiling', async () => {
  const h = harness({ maxQueueWaitMs: 500 });
  const first = h.compile('a');
  const stale = outcome(h.compile('stale'));
  const [worker] = h.workers;
  h.advance(500);
  assert.deepEqual(await stale, { error: 'Cube schema validation waited 500ms without starting' });
  const fresh = h.compile('fresh');
  worker.reply(0, { result: 'a' });
  await first;
  assert.deepEqual(worker.posted.map((m) => m.schema), ['a', 'fresh']);
  worker.reply(1, { result: 'fresh' });
  assert.equal(await fresh, 'fresh');
});

test('an unexpected worker exit rejects everything and exits the process', async () => {
  const h = harness();
  const running = outcome(h.compile('a'));
  const queued = outcome(h.compile('b'));
  const errors = [];
  const original = console.error;
  console.error = (error) => errors.push(error);
  try {
    h.workers[0].emit('exit', 0);
  } finally {
    console.error = original;
  }
  assert.deepEqual(h.exits, [1]);
  assert.match((await running).error, /exited with code 0/);
  assert.match((await queued).error, /exited with code 0/);
  assert.equal(errors.length, 1);
});

test('a dispatched request is no longer subject to its queue deadline', async () => {
  const h = harness({ maxQueueWaitMs: 500 });
  const first = h.compile('a');
  const second = h.compile('b');
  h.advance(400);
  h.workers[0].reply(0, { result: 'a' });
  await first;
  h.advance(900);
  h.workers[0].reply(1, { result: 'b' });
  assert.equal(await second, 'b');
});

test('the default queue deadline leaves a full compile inside the caller budget', () => {
  assert.equal(defaultMaxQueueWaitMs(60000) + 60000, 70000);
  assert.equal(defaultMaxQueueWaitMs(30000), 40000);
  assert.equal(defaultMaxQueueWaitMs(90000), 1000);
});

test('malformed timeout settings fail startup instead of disabling the bounds', () => {
  for (const name of ['CUBE_VALIDATOR_COMPILE_TIMEOUT_MS', 'CUBE_VALIDATOR_MAX_QUEUE_WAIT_MS']) {
    for (const value of ['abc', '0', '-5', '1.5']) {
      process.env[name] = value;
      try {
        assert.throws(() => createWorkerCompiler({ workerFactory: () => new FakeWorker() }), new RegExp(name));
      } finally {
        delete process.env[name];
      }
    }
  }
});

test('a request that arrives after the worker could not restart is rejected, not left hanging', async () => {
  const h = harness({ failRestart: true });
  const original = console.error;
  console.error = () => {};
  try {
    const slow = outcome(h.compile('slow'));
    h.advance(1000);
    await slow;
    const during = outcome(h.compile('during restart'));
    await settle();
    await settle();
    assert.match((await during).error, /synthetic restart failure|not running/);
    assert.deepEqual(await outcome(h.compile('after')), { error: 'Cube validator worker is not running' });
  } finally {
    console.error = original;
  }
});

test('a failed restart exits and dispatches nothing to a missing worker', async () => {
  const h = harness({ failRestart: true });
  const slow = outcome(h.compile('slow'));
  const queued = outcome(h.compile('queued'));
  const original = console.error;
  console.error = () => {};
  try {
    h.advance(1000);
    await settle();
    await settle();
  } finally {
    console.error = original;
  }
  assert.match((await slow).error, /timed out/);
  assert.deepEqual(h.exits, [1]);
  assert.deepEqual(await queued, { error: 'synthetic restart failure' });
});
