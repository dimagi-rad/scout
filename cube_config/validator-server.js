const path = require('path');
const crypto = require('crypto');
const { Worker } = require('worker_threads');
const {
  createRedisClient,
  createValidatorApp,
} = require('./validator-core');

const DEFAULT_COMPILE_TIMEOUT_MS = 60 * 1000;

function createWorkerCompiler(options = {}) {
  const workerFactory = options.workerFactory || (() => new Worker(path.join(__dirname, 'validator-worker.js')));
  let worker = null;
  let restartPromise = null;
  const pending = new Map();

  function rejectPending(error) {
    for (const request of pending.values()) {
      clearTimeout(request.timeout);
      request.reject(error);
    }
    pending.clear();
  }

  function exitAfterWorkerFailure(error, exitCode = 1) {
    rejectPending(error);
    console.error(error);
    process.exit(exitCode);
  }

  function startWorker() {
    worker = workerFactory();

    worker.on('message', (message) => {
      const request = pending.get(message.id);
      if (!request) {
        return;
      }

      clearTimeout(request.timeout);
      pending.delete(message.id);

      if (message.error) {
        request.reject(new Error(message.error));
      } else {
        request.resolve(message.result);
      }
    });

    worker.on('error', (error) => {
      exitAfterWorkerFailure(error);
    });

    worker.on('exit', (code) => {
      if (restartPromise) {
        return;
      }
      exitAfterWorkerFailure(
        new Error(`Cube validator worker exited with code ${code}`),
        code === 0 ? 1 : code
      );
    });
  }

  async function restartWorker(error) {
    if (restartPromise) {
      return restartPromise;
    }

    const workerToTerminate = worker;
    rejectPending(error);

    restartPromise = (async () => {
      try {
        await workerToTerminate.terminate();
        startWorker();
      } catch (terminateError) {
        exitAfterWorkerFailure(terminateError);
      } finally {
        restartPromise = null;
      }
    })();

    return restartPromise;
  }

  startWorker();

  return async function compileSchema(schema) {
    if (restartPromise) {
      await restartPromise;
    }

    const activeWorker = worker;
    const id = crypto.randomUUID();
    const timeoutMs = Number(process.env.CUBE_VALIDATOR_COMPILE_TIMEOUT_MS || DEFAULT_COMPILE_TIMEOUT_MS);

    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        const error = new Error(`Cube schema validation timed out after ${timeoutMs}ms`);
        restartWorker(error).catch(exitAfterWorkerFailure);
      }, timeoutMs);

      pending.set(id, { resolve, reject, timeout });
      try {
        activeWorker.postMessage({ id, schema });
      } catch (error) {
        clearTimeout(timeout);
        pending.delete(id);
        reject(error);
      }
    });
  };
}

async function main() {
  const redis = createRedisClient(process.env.REDIS_URL);
  if (redis) {
    try {
      await redis.connect();
    } catch (error) {
      console.warn({ error: error.message }, 'Cube validator Redis connect failed; continuing without Redis cache');
    }
  }

  const app = createValidatorApp({
    compileSchema: createWorkerCompiler(),
    redis,
    authSecret: process.env.CUBE_VALIDATOR_SECRET || process.env.CUBEJS_API_SECRET,
  });

  const port = Number(process.env.CUBE_VALIDATOR_PORT || 4010);
  await app.listen({ host: '0.0.0.0', port });
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}

module.exports = {
  createWorkerCompiler,
  main,
};
