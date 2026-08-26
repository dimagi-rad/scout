const crypto = require('crypto');
const Fastify = require('fastify');
const Redis = require('ioredis');
const { LRUCache } = require('lru-cache');

const COMPILER_VERSION = '1.6.39';
const VALIDATOR_VERSION = 'v1';
const DEFAULT_BODY_LIMIT_BYTES = 10 * 1024 * 1024;
const DEFAULT_RESULT_CACHE_MAX = 2000;
const DEFAULT_REDIS_TTL_SECONDS = 7 * 24 * 60 * 60;
const DEFAULT_REDIS_LOCK_TTL_MS = 65 * 1000;
const DEFAULT_REDIS_LOCK_WAIT_MS = 60 * 1000;
const DEFAULT_REDIS_LOCK_POLL_MS = 100;

function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function buildCacheKey(schemaHash) {
  return `cube_schema_validator:v1:compiler:${COMPILER_VERSION}:validator:${VALIDATOR_VERSION}:sha256:${schemaHash}`;
}

function sanitizeCachedResult(result) {
  return {
    valid: Boolean(result.valid),
    errors: Array.isArray(result.errors) ? result.errors : [],
    compiler_version: result.compiler_version || COMPILER_VERSION,
    validator_version: result.validator_version || VALIDATOR_VERSION,
  };
}

function withResponseMetadata(result, schemaHash, cache, startedAt) {
  return {
    ...sanitizeCachedResult(result),
    schema_hash: schemaHash,
    cache,
    duration_ms: Date.now() - startedAt,
  };
}

function serviceFailureResult(error) {
  const message = error && error.message ? error.message : String(error);
  return {
    valid: false,
    errors: [
      {
        message,
        cube_name: null,
        full_message: message,
      },
    ],
    compiler_version: COMPILER_VERSION,
    validator_version: VALIDATOR_VERSION,
  };
}

function createRedisClient(redisUrl, logger = console) {
  if (!redisUrl) {
    return null;
  }

  const redis = new Redis(redisUrl, {
    maxRetriesPerRequest: 1,
    enableOfflineQueue: false,
    lazyConnect: true,
  });

  redis.on('error', (error) => {
    logger.warn({ error: error.message }, 'Cube validator Redis error');
  });

  return redis;
}

async function getRedisValue(redis, key, logger) {
  if (!redis) {
    return null;
  }

  try {
    const raw = await redis.get(key);
    return raw ? JSON.parse(raw) : null;
  } catch (error) {
    logger.warn({ error: error.message, key }, 'Cube validator Redis read failed');
    return null;
  }
}

async function setRedisValue(redis, key, value, ttlSeconds, logger) {
  if (!redis) {
    return;
  }

  try {
    await redis.set(key, JSON.stringify(value), 'EX', ttlSeconds);
  } catch (error) {
    logger.warn({ error: error.message, key }, 'Cube validator Redis write failed');
  }
}

async function releaseRedisLock(redis, lockKey, token, logger) {
  if (!redis) {
    return;
  }

  try {
    await redis.eval(
      "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
      1,
      lockKey,
      token
    );
  } catch (error) {
    logger.warn({ error: error.message, lockKey }, 'Cube validator Redis lock release failed');
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function authMatches(request, expectedSecret) {
  if (!expectedSecret) {
    return true;
  }

  const authorization = request.headers.authorization || '';
  if (authorization === `Bearer ${expectedSecret}`) {
    return true;
  }

  return request.headers['x-internal-token'] === expectedSecret;
}

function createValidatorApp(options) {
  const compileSchema = options.compileSchema;
  const redis = options.redis || null;
  const logger = options.logger === undefined ? true : options.logger;
  const authSecret = options.authSecret || null;
  const redisTtlSeconds = options.redisTtlSeconds || DEFAULT_REDIS_TTL_SECONDS;
  const redisLockTtlMs = options.redisLockTtlMs || DEFAULT_REDIS_LOCK_TTL_MS;
  const redisLockWaitMs = options.redisLockWaitMs || DEFAULT_REDIS_LOCK_WAIT_MS;
  const redisLockPollMs = options.redisLockPollMs || DEFAULT_REDIS_LOCK_POLL_MS;
  const resultCache = options.resultCache || new LRUCache({
    max: Number(process.env.CUBE_VALIDATOR_RESULT_CACHE_MAX || DEFAULT_RESULT_CACHE_MAX),
  });
  const inFlight = new Map();

  const app = Fastify({
    logger,
    bodyLimit: Number(process.env.CUBE_VALIDATOR_BODY_LIMIT_BYTES || DEFAULT_BODY_LIMIT_BYTES),
  });

  async function cacheResult(cacheKey, result) {
    const cachedResult = sanitizeCachedResult(result);
    resultCache.set(cacheKey, cachedResult);
    await setRedisValue(redis, cacheKey, cachedResult, redisTtlSeconds, app.log);
    return cachedResult;
  }

  async function compileAndCache(cacheKey, schemaContent) {
    const result = await compileSchema(schemaContent);
    return cacheResult(cacheKey, result);
  }

  async function compileWithRedisLock(cacheKey, schemaContent) {
    if (!redis) {
      return compileAndCache(cacheKey, schemaContent);
    }

    const lockKey = `${cacheKey}:lock`;
    const token = crypto.randomUUID();
    let lockAcquired = false;

    try {
      lockAcquired = (await redis.set(lockKey, token, 'PX', redisLockTtlMs, 'NX')) === 'OK';
    } catch (error) {
      app.log.warn({ error: error.message, lockKey }, 'Cube validator Redis lock acquire failed');
      return compileAndCache(cacheKey, schemaContent);
    }

    if (lockAcquired) {
      try {
        return await compileAndCache(cacheKey, schemaContent);
      } finally {
        await releaseRedisLock(redis, lockKey, token, app.log);
      }
    }

    const waitStartedAt = Date.now();
    while (Date.now() - waitStartedAt < redisLockWaitMs) {
      const cached = await getRedisValue(redis, cacheKey, app.log);
      if (cached) {
        const sanitized = sanitizeCachedResult(cached);
        resultCache.set(cacheKey, sanitized);
        return sanitized;
      }
      await sleep(redisLockPollMs);
    }

    app.log.warn({ cacheKey }, 'Cube validator Redis lock wait expired; compiling locally');
    return compileAndCache(cacheKey, schemaContent);
  }

  async function validateSchema(schemaContent) {
    const schemaHash = sha256(schemaContent);
    const cacheKey = buildCacheKey(schemaHash);

    const memoryHit = resultCache.get(cacheKey);
    if (memoryHit) {
      return { result: sanitizeCachedResult(memoryHit), schemaHash, cache: 'memory' };
    }

    const redisHit = await getRedisValue(redis, cacheKey, app.log);
    if (redisHit) {
      const sanitized = sanitizeCachedResult(redisHit);
      resultCache.set(cacheKey, sanitized);
      return { result: sanitized, schemaHash, cache: 'redis' };
    }

    if (!inFlight.has(cacheKey)) {
      inFlight.set(
        cacheKey,
        compileWithRedisLock(cacheKey, schemaContent).finally(() => {
          inFlight.delete(cacheKey);
        })
      );
    }

    const result = await inFlight.get(cacheKey);
    return { result, schemaHash, cache: 'compiled' };
  }

  app.get('/readyz', async () => ({
    status: 'ok',
    compiler_version: COMPILER_VERSION,
    validator_version: VALIDATOR_VERSION,
    redis: redis ? redis.status : 'disabled',
  }));

  app.post('/internal/validate-cube-schema', async (request, reply) => {
    const startedAt = Date.now();

    if (!authMatches(request, authSecret)) {
      return reply.code(401).send({ error: 'unauthorized' });
    }

    const schemaContent = request.body && typeof request.body.schema === 'string'
      ? request.body.schema
      : '';

    if (!schemaContent.trim()) {
      return withResponseMetadata(
        { valid: true, errors: [] },
        sha256(schemaContent),
        'empty',
        startedAt
      );
    }

    let validation;
    try {
      validation = await validateSchema(schemaContent);
    } catch (error) {
      const schemaHash = sha256(schemaContent);
      request.log.error(
        { error: error.message, schema_hash: schemaHash },
        'Cube validator service failure'
      );
      return reply.code(503).send(withResponseMetadata(
        serviceFailureResult(error),
        schemaHash,
        'service_error',
        startedAt
      ));
    }

    return withResponseMetadata(
      validation.result,
      validation.schemaHash,
      validation.cache,
      startedAt
    );
  });

  app.decorate('validatorState', {
    resultCache,
    inFlight,
    validateSchema,
  });

  return app;
}

module.exports = {
  COMPILER_VERSION,
  VALIDATOR_VERSION,
  buildCacheKey,
  createRedisClient,
  createValidatorApp,
  sanitizeCachedResult,
  serviceFailureResult,
  sha256,
};
