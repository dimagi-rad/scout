const { Pool } = require('pg');
const { createHash } = require('node:crypto');

const IDENTIFIER_RE = /^[a-z][a-z0-9_]*$/;

function sslConfigForUrl(rawUrl) {
  if (!rawUrl) {
    return false;
  }
  const parsed = new URL(rawUrl);
  const host = parsed.hostname;
  if (!host || host === 'localhost' || host === '127.0.0.1' || host === 'platform-db') {
    return false;
  }
  return { rejectUnauthorized: false };
}

function connectionFromUrl(rawUrl) {
  const parsed = new URL(rawUrl);
  return {
    host: parsed.hostname || 'localhost',
    port: Number(parsed.port || 5432),
    database: parsed.pathname.replace(/^\//, '') || 'scout',
    user: decodeURIComponent(parsed.username || ''),
    password: decodeURIComponent(parsed.password || ''),
    ssl: sslConfigForUrl(rawUrl),
  };
}

function requireIdentifier(value, label) {
  if (typeof value !== 'string' || !IDENTIFIER_RE.test(value)) {
    throw new Error(`Invalid ${label} in Cube security context`);
  }
  return value;
}

function workspaceContext(securityContext) {
  // Cube's unauthenticated internal readiness checks have no tenant context.
  // A partially populated tenant context must never use the unscoped driver.
  if (!securityContext || Object.keys(securityContext).length === 0) {
    return null;
  }
  for (const field of ['workspaceId', 'semanticModelId']) {
    if (typeof securityContext[field] !== 'string' || !securityContext[field]) {
      throw new Error(`Missing ${field} in Cube security context`);
    }
  }
  return [
    securityContext.workspaceId,
    securityContext.semanticModelId,
    requireIdentifier(securityContext.schemaName, 'schemaName'),
    requireIdentifier(securityContext.readonlyRole, 'readonlyRole'),
  ];
}

function contextId(prefix, parts) {
  // Hash the full tuple: truncating concatenated UUIDs + schema hashes can
  // discard the physical-schema suffix that distinguishes blue-green swaps.
  return `${prefix}_${createHash('sha256').update(JSON.stringify(parts)).digest('hex')}`;
}

const appDatabaseUrl = process.env.DATABASE_URL || 'postgresql://platform:devpassword@platform-db:5432/agent_platform';
const managedDatabaseUrl = process.env.MANAGED_DATABASE_URL || appDatabaseUrl;
const appPool = new Pool({ connectionString: appDatabaseUrl, ssl: sslConfigForUrl(appDatabaseUrl) });
const managedConfig = connectionFromUrl(managedDatabaseUrl);

module.exports = {
  contextToAppId: ({ securityContext }) => {
    const context = workspaceContext(securityContext);
    if (!context) {
      return 'scout_healthcheck';
    }
    return contextId('scout_app_v1', [...context, securityContext.cubeSchemaHash || 'unknown']);
  },

  contextToOrchestratorId: ({ securityContext }) => {
    const context = workspaceContext(securityContext);
    if (!context) {
      return 'scout_healthcheck';
    }
    // Cube caches database drivers, query queues, and result caches by
    // orchestrator ID, independently of contextToAppId's compiler cache.
    // Reuse within the same physical/authorization scope, never across it.
    // Schema-content changes do not require another database connection pool.
    const [workspaceId, , schemaName, readonlyRole] = context;
    return contextId('scout_data_v1', [workspaceId, schemaName, readonlyRole]);
  },

  dbType: () => 'postgres',

  driverFactory: ({ securityContext }) => {
    const context = workspaceContext(securityContext);
    if (!context) {
      return { type: 'postgres', ...managedConfig };
    }

    const [, , schemaName, readonlyRole] = context;
    return {
      type: 'postgres',
      ...managedConfig,
      options: `-c role=${readonlyRole} -c search_path=${schemaName},public -c statement_timeout=30000`,
    };
  },

  repositoryFactory: ({ securityContext }) => ({
    dataSchemaFiles: async () => {
      if (!securityContext?.workspaceId || !securityContext?.semanticModelId) {
        return [];
      }

      const { rows } = await appPool.query(
        `
          SELECT filename, content
          FROM semantic_cubeschema
          WHERE workspace_id = $1
            AND semantic_model_id = $2
            AND status = 'active'
          ORDER BY updated_at DESC
          LIMIT 1
        `,
        [securityContext.workspaceId, securityContext.semanticModelId]
      );

      return rows.map((row) => ({
        fileName: row.filename,
        content: row.content,
      }));
    },
  }),

  schemaVersion: async ({ securityContext }) => {
    if (!securityContext?.workspaceId || !securityContext?.semanticModelId) {
      return 'healthcheck';
    }

    const { rows } = await appPool.query(
      `
        SELECT content_hash, updated_at
        FROM semantic_cubeschema
        WHERE workspace_id = $1
          AND semantic_model_id = $2
          AND status = 'active'
        ORDER BY updated_at DESC
        LIMIT 1
      `,
      [securityContext.workspaceId, securityContext.semanticModelId]
    );

    if (!rows[0]) {
      return 'none';
    }
    return `${rows[0].content_hash}:${rows[0].updated_at.toISOString()}`;
  },

  scheduledRefreshTimer: false,
  scheduledRefreshContexts: () => [],
};
