const { Pool } = require('pg');
const { PostgresDriver } = require('@cubejs-backend/postgres-driver');
const { createHash } = require('node:crypto');
const { createConnectionSlots, positiveIntegerFromEnv } = require('./connection-slots');

const IDENTIFIER_RE = /^[a-z][a-z0-9_]*$/;
const PUBLICATION_REVISION = Symbol('scoutPublicationRevision');
const CATALOG_QUERY_TIMEOUT_MS = 5000;
const DRIVER_STATEMENT_TIMEOUT_MS = 30000;
// Prod and staging share one RDS instance that has already run out of
// connections, so every pool here is capped and sheds idle connections quickly.
// Cube's Postgres query queue runs two queries per orchestrator at a time.
const DRIVER_POOL_MAX = 2;
const DRIVER_IDLE_TIMEOUT_MS = 10000;
const DRIVER_EVICTION_INTERVAL_MS = 5000;
const CATALOG_POOL_MAX = 3;
// Created, with SELECT on semantic_cubeschema only, by semantic migration 0005.
const CATALOG_ROLE = 'scout_cube_catalog';
const CATALOG_ROLE_PROBE_INTERVAL_MS = 60000;
const driverSlots = createConnectionSlots(
  positiveIntegerFromEnv(process.env, 'SCOUT_CUBE_MAX_DRIVER_CONNECTIONS', 16)
);

// Counts every tenant connection, including the unpooled one Cube opens for
// testConnection(), against one process-wide limit.
class SlottedPostgresDriver extends PostgresDriver {
  async createConnection(poolConfig, poolName) {
    const release = await driverSlots.acquire();
    let client;
    try {
      client = await super.createConnection(poolConfig, poolName);
    } catch (error) {
      release();
      throw error;
    }
    client.once('end', release);
    return client;
  }
}

function boundedPoolConfig(maxPoolSize) {
  return {
    maxPoolSize,
    idleTimeoutMillis: DRIVER_IDLE_TIMEOUT_MS,
    softIdleTimeoutMillis: DRIVER_IDLE_TIMEOUT_MS,
    evictionRunIntervalMillis: DRIVER_EVICTION_INTERVAL_MS,
  };
}

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
const catalogPoolOptions = {
  connectionString: appDatabaseUrl,
  ssl: sslConfigForUrl(appDatabaseUrl),
  connectionTimeoutMillis: CATALOG_QUERY_TIMEOUT_MS,
  statement_timeout: CATALOG_QUERY_TIMEOUT_MS,
  query_timeout: CATALOG_QUERY_TIMEOUT_MS,
};
const ownerPool = new Pool({ ...catalogPoolOptions, max: 1 });
let rolePool = null;
let roleProbe = null;
let nextRoleProbeAt = 0;

// Cube deploys before the API applies migrations, so on the deploy that adds
// the role it is missing for a few minutes. Until then, read as the owner, as
// before; once the role exists every catalog read uses it, and a broken grant
// fails closed rather than falling back.
async function catalogPool() {
  if (rolePool) {
    return rolePool;
  }
  if (Date.now() >= nextRoleProbeAt) {
    roleProbe ??= ownerPool
      .query('SELECT to_regrole($1) IS NOT NULL AS present', [CATALOG_ROLE])
      .then(({ rows }) => {
        if (rows[0]?.present) {
          rolePool ??= new Pool({
            ...catalogPoolOptions,
            max: CATALOG_POOL_MAX,
            options: `-c role=${CATALOG_ROLE} -c default_transaction_read_only=on`,
          });
        } else {
          nextRoleProbeAt = Date.now() + CATALOG_ROLE_PROBE_INTERVAL_MS;
          console.warn(`Cube catalog role ${CATALOG_ROLE} does not exist yet; reading semantic_cubeschema as the DATABASE_URL owner`);
        }
      })
      .finally(() => {
        roleProbe = null;
      });
    await roleProbe;
  }
  return rolePool ?? ownerPool;
}

async function catalogQuery(text, values) {
  return (await catalogPool()).query(text, values);
}
const managedConfig = connectionFromUrl(managedDatabaseUrl);

module.exports = {
  queryRewrite: async (query, context) => {
    const { securityContext } = context;
    if (!workspaceContext(securityContext)) {
      return query;
    }
    // One authoritative lookup per request also supports JWTs from an older
    // API during Cube-first deployments. Never cache this across publications.
    context[PUBLICATION_REVISION] ??= catalogQuery(
      `
        SELECT to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS data_revision
        FROM semantic_cubeschema
        WHERE workspace_id = $1
          AND semantic_model_id = $2
          AND status = 'active'
        ORDER BY updated_at DESC
        LIMIT 1
      `,
      [securityContext.workspaceId, securityContext.semanticModelId]
    ).then(({ rows }) => {
      if (!rows[0]?.data_revision) {
        throw new Error('No active Cube schema for this workspace');
      }
      return rows[0].data_revision;
    });
    securityContext.cubeDataRevision = await context[PUBLICATION_REVISION];
    return query;
  },

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

  driverFactory: ({ securityContext, dataSource = 'default' }) => {
    const context = workspaceContext(securityContext);
    if (!context) {
      // Cube's standalone /readyz runs testConnection() through this driver, so
      // it must connect; there is no tenant role to downgrade to (#421). These are
      // session defaults, not enforcement: the guarantee is still that no model is
      // served for this context, and these only bound what one could reach.
      // It stays outside the tenant slots so tenant load cannot fail readiness.
      return new PostgresDriver({
        ...managedConfig,
        ...boundedPoolConfig(1),
        dataSource,
        options: `-c statement_timeout=${DRIVER_STATEMENT_TIMEOUT_MS} -c default_transaction_read_only=on -c search_path=pg_catalog`,
      });
    }

    const [, , schemaName, readonlyRole] = context;
    return new SlottedPostgresDriver({
      ...managedConfig,
      ...boundedPoolConfig(DRIVER_POOL_MAX),
      dataSource,
      options: `-c role=${readonlyRole} -c search_path=${schemaName},public -c statement_timeout=${DRIVER_STATEMENT_TIMEOUT_MS}`,
    });
  },

  repositoryFactory: ({ securityContext }) => ({
    dataSchemaFiles: async () => {
      if (!securityContext?.workspaceId || !securityContext?.semanticModelId) {
        return [];
      }

      const { rows } = await catalogQuery(
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

    const { rows } = await catalogQuery(
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
