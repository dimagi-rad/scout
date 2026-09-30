const path = require('path');
const crypto = require('crypto');
const { Worker } = require('worker_threads');

const DEFAULT_COMPILE_TIMEOUT_MS = 60 * 1000;
// VALIDATE_BUDGET_SECONDS in apps/semantic/services/cube_client.py. Queueing
// longer than the budget minus a full compile cannot answer that request, so it
// gets a prompt 503 instead, which the client retries as a fresh request.
const CALLER_BUDGET_MS = 70 * 1000;
const MIN_QUEUE_WAIT_MS = 1000;

function positiveIntegerFromEnv(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined || raw === '') {
    return fallback;
  }
  if (!/^[1-9][0-9]*$/.test(raw) || !Number.isSafeInteger(Number(raw))) {
    throw new Error(`${name} must be a positive integer number of milliseconds`);
  }
  return Number(raw);
}

function defaultMaxQueueWaitMs(timeoutMs) {
  return Math.max(MIN_QUEUE_WAIT_MS, CALLER_BUDGET_MS - timeoutMs);
}

// Compiles run one at a time on a single worker thread, and each request's
// timer starts when its compile is dispatched. Timing from enqueue let
// concurrent compiles interleave past the timeout, and the restart then
// rejected every pending request with it (#720).
function createWorkerCompiler(options = {}) {
  const workerFactory = options.workerFactory || (() => new Worker(path.join(__dirname, 'validator-worker.js')));
  const timeoutMs = options.timeoutMs
    ?? positiveIntegerFromEnv('CUBE_VALIDATOR_COMPILE_TIMEOUT_MS', DEFAULT_COMPILE_TIMEOUT_MS);
  const maxQueueWaitMs = options.maxQueueWaitMs
    ?? positiveIntegerFromEnv('CUBE_VALIDATOR_MAX_QUEUE_WAIT_MS', defaultMaxQueueWaitMs(timeoutMs));
  const schedule = options.setTimeout || setTimeout;
  const unschedule = options.clearTimeout || clearTimeout;
  const exit = options.exit || ((code) => process.exit(code));
  const queue = [];
  let worker = null;
  let active = null;
  let restarting = null;

  function rejectAll(error) {
    const requests = active ? [active, ...queue] : [...queue];
    for (const request of queue) {
      unschedule(request.queueTimeout);
    }
    if (active) {
      unschedule(active.timeout);
    }
    active = null;
    queue.length = 0;
    for (const request of requests) {
      request.reject(error);
    }
  }

  function exitAfterWorkerFailure(error, exitCode = 1) {
    rejectAll(error);
    console.error(error);
    exit(exitCode);
  }

  function finish(request, settle) {
    if (active !== request) {
      return;
    }
    unschedule(request.timeout);
    active = null;
    settle();
    dispatchNext();
  }

  function startWorker() {
    const current = workerFactory();
    worker = current;

    current.on('message', (message) => {
      if (current !== worker || !active || message.id !== active.id) {
        return;
      }
      const request = active;
      finish(request, () => {
        if (message.error) {
          request.reject(new Error(message.error));
        } else {
          request.resolve(message.result);
        }
      });
    });

    current.on('error', (error) => {
      if (current === worker) {
        exitAfterWorkerFailure(error);
      } else {
        console.error(error);
      }
    });

    current.on('exit', (code) => {
      if (current !== worker) {
        return;
      }
      exitAfterWorkerFailure(
        new Error(`Cube validator worker exited with code ${code}`),
        code === 0 ? 1 : code
      );
    });
  }

  function restartWorker() {
    const workerToTerminate = worker;
    // Detach first: terminate() makes the old worker emit 'exit', which must not
    // read as a crash.
    worker = null;
    restarting = workerToTerminate.terminate()
      .then(startWorker)
      .catch((error) => exitAfterWorkerFailure(error))
      .finally(() => {
        restarting = null;
        dispatchNext();
      });
  }

  function timeOut(request) {
    if (active !== request) {
      return;
    }
    active = null;
    request.reject(new Error(`Cube schema validation timed out after ${timeoutMs}ms`));
    restartWorker();
  }

  function dispatchNext() {
    while (!active && !restarting && queue.length) {
      if (!worker) {
        rejectAll(new Error('Cube validator worker is not running'));
        return;
      }
      const request = queue.shift();
      unschedule(request.queueTimeout);
      active = request;
      request.timeout = schedule(() => timeOut(request), timeoutMs);
      try {
        worker.postMessage({ id: request.id, schema: request.schema });
      } catch (error) {
        unschedule(request.timeout);
        active = null;
        request.reject(error);
      }
    }
  }

  startWorker();

  return function compileSchema(schema) {
    return new Promise((resolve, reject) => {
      const request = { id: crypto.randomUUID(), schema, resolve, reject };
      request.queueTimeout = schedule(() => {
        const index = queue.indexOf(request);
        if (index !== -1) {
          queue.splice(index, 1);
          reject(new Error(`Cube schema validation waited ${maxQueueWaitMs}ms without starting`));
        }
      }, maxQueueWaitMs);
      queue.push(request);
      dispatchNext();
    });
  };
}

module.exports = { createWorkerCompiler, defaultMaxQueueWaitMs, DEFAULT_COMPILE_TIMEOUT_MS };
