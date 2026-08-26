const { parentPort } = require('worker_threads');
const { NativeInstance } = require('@cubejs-backend/native');
const { prepareCompiler } = require('@cubejs-backend/schema-compiler');
const { LRUCache } = require('lru-cache');
const { COMPILER_VERSION, VALIDATOR_VERSION } = require('./validator-core');

const nativeInstance = new NativeInstance();
const compiledScriptCache = new LRUCache({ max: 500 });
const compiledYamlCache = new LRUCache({ max: 500 });
const compiledJinjaCache = new LRUCache({ max: 500 });

function parseCubeName(message, yamlContent) {
  const match = message.match(/^(.+?) cube: /);
  if (match) {
    return match[1];
  }

  const undefinedMatch = message.match(/^(\w+) is not defined/);
  if (undefinedMatch && yamlContent) {
    const identifier = undefinedMatch[1];
    let currentCube = null;

    for (const line of yamlContent.split('\n')) {
      const topLevel = line.match(/^\s*-\s+name:\s*(\S+)/);
      if (topLevel) {
        currentCube = topLevel[1];
        continue;
      }

      if (currentCube && line.includes(identifier)) {
        return currentCube;
      }
    }
  }

  return null;
}

async function validate(schemaContent) {
  try {
    const repo = {
      dataSchemaFiles: async () => [
        { fileName: 'schema.yaml', content: schemaContent },
      ],
    };

    const compilers = prepareCompiler(repo, {
      omitErrors: true,
      nativeInstance,
      compiledScriptCache,
      compiledYamlCache,
      compiledJinjaCache,
    });

    await compilers.compiler.compile();

    const rawErrors = compilers.compiler.errorsReporter.getErrors() || [];
    const errors = rawErrors.map((error) => {
      const fullMessage = error.message || String(error);
      const cubeName = parseCubeName(fullMessage, schemaContent);

      return {
        message: cubeName
          ? fullMessage.replace(/^.+? cube: /, '')
          : fullMessage,
        cube_name: cubeName,
        full_message: fullMessage,
      };
    });

    return {
      valid: errors.length === 0,
      errors,
      compiler_version: COMPILER_VERSION,
      validator_version: VALIDATOR_VERSION,
    };
  } catch (error) {
    const message = error.message || String(error);
    const serviceError = new Error(message);
    serviceError.name = 'CubeValidatorServiceError';
    serviceError.cause = error;
    throw serviceError;
  }
}

if (parentPort) {
  parentPort.on('message', async (message) => {
    const { id, schema } = message;
    try {
      parentPort.postMessage({ id, result: await validate(schema || '') });
    } catch (error) {
      parentPort.postMessage({
        id,
        error: error.message || String(error),
      });
    }
  });
}

module.exports = {
  parseCubeName,
  validate,
};
