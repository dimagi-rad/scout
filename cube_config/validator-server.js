const {
  createRedisClient,
  createValidatorApp,
} = require('./validator-core');
const { createWorkerCompiler } = require('./validator-compiler');

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

module.exports = { main };
