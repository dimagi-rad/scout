'use strict';

function positiveIntegerFromEnv(env, key, fallback) {
  const raw = env[key];
  if (raw === undefined || raw === '') {
    return fallback;
  }
  if (!/^[1-9][0-9]*$/.test(raw) || !Number.isSafeInteger(Number(raw))) {
    throw new Error(`${key} must be a positive safe integer`);
  }
  return Number(raw);
}

// A process-wide FIFO counting semaphore. Each tenant driver has its own pool,
// so per-pool maxima alone would still multiply with the number of workspaces.
function createConnectionSlots(limit, maxWaitMs, {
  setTimeout: schedule = setTimeout,
  clearTimeout: unschedule = clearTimeout,
} = {}) {
  if (!Number.isSafeInteger(limit) || limit <= 0) {
    throw new Error('Connection slot limit must be a positive safe integer');
  }
  let inUse = 0;
  const waiters = [];

  function grant() {
    inUse += 1;
    let released = false;
    return () => {
      if (released) {
        return;
      }
      released = true;
      inUse -= 1;
      const next = waiters.shift();
      if (next) {
        unschedule(next.timer);
        next.resolve(grant());
      }
    };
  }

  return {
    acquire() {
      if (inUse < limit) {
        return Promise.resolve(grant());
      }
      return new Promise((resolve, reject) => {
        const waiter = { resolve };
        waiter.timer = schedule(() => {
          waiters.splice(waiters.indexOf(waiter), 1);
          reject(new Error(`No Cube database connection slot freed within ${maxWaitMs}ms (limit ${limit})`));
        }, maxWaitMs);
        waiters.push(waiter);
      });
    },
  };
}

module.exports = { createConnectionSlots, positiveIntegerFromEnv };
