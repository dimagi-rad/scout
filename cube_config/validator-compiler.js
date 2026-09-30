const path = require('path');
const crypto = require('crypto');
const { Worker } = require('worker_threads');

const DEFAULT_COMPILE_TIMEOUT_MS = 60 * 1000;
// Scout's client gives a validation 70s in all (VALIDATE_BUDGET_SECONDS), and a
// dispatched compile may take the full 60s, so only the first 10s of queueing
// can still produce an answer the caller receives.
const DEFAULT_MAX_QUEUE_WAIT_MS = 10 * 1000;

// Compiles run one at a time on a single worker thread, and each request's
// timer starts when its compile is dispatched. Timing from enqueue let
// concurrent compiles interleave past the timeout, and the restart then
// rejected every pending request with it (#720).
function createWorkerCompiler(options = {}) {
  const workerFactory = options.workerFactory || (() => new Worker(path.join(__dirname, 'validator-worker.js')));
  const timeoutMs = options.timeoutMs
    ?? Number(process.env.CUBE_VALIDATOR_COMPILE_TIMEOUT_MS || DEFAULT_COMPILE_TIMEOUT_MS);
  const maxQueueWaitMs = options.maxQueueWaitMs
    ?? Number(process.env.CUBE_VALIDATOR_MAX_QUEUE_WAIT_MS || DEFAULT_MAX_QUEUE_WAIT_MS);
  const now = options.now || Date.now;
  const schedule = options.setTimeout || setTimeout;
  const unschedule = options.clearTimeout || clearTimeout;
  const exit = options.exit || ((code) => process.exit(code));
  const queue = [];
  let worker = null;
  let active = null;
  let restarting = null;

  function rejectAll(error) {
    const requests = active ? [active, ...queue] : [...queue];
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
        if (worker) {
          dispatchNext();
        }
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
      const request = queue.shift();
      if (now() - request.enqueuedAt >= maxQueueWaitMs) {
        request.reject(new Error(`Cube schema validation waited at least ${maxQueueWaitMs}ms to start`));
        continue;
      }
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
      queue.push({ id: crypto.randomUUID(), schema, resolve, reject, enqueuedAt: now() });
      dispatchNext();
    });
  };
}

module.exports = { createWorkerCompiler, DEFAULT_COMPILE_TIMEOUT_MS, DEFAULT_MAX_QUEUE_WAIT_MS };
