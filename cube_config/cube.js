const { Pool } = require('pg');
const { PostgresDriver } = require('@cubejs-backend/postgres-driver');
const { createHash } = require('node:crypto');
const { readFileSync } = require('node:fs');
const { createConnectionSlots, positiveIntegerFromEnv } = require('./connection-slots');

const IDENTIFIER_RE = /^[a-z][a-z0-9_]*$/;
const PUBLICATION_REVISION = Symbol('scoutPublicationRevision');
const CATALOG_QUERY_TIMEOUT_MS = 5000;
const DRIVER_STATEMENT_TIMEOUT_MS = 30000;
// Prod's services share one RDS instance that has already run out of
// connections, so every pool here is capped and sheds idle connections quickly.
// Cube's Postgres query queue runs two queries per orchestrator at a time.
const DRIVER_POOL_MAX = 2;
const DRIVER_IDLE_TIMEOUT_MS = 10000;
const DRIVER_EVICTION_INTERVAL_MS = 5000;
const CATALOG_POOL_MAX = 3;
// Created, with SELECT on semantic_cubeschema only, by semantic migration 0005.
const CATALOG_ROLE = 'scout_cube_catalog';
const CATALOG_ROLE_PROBE_INTERVAL_MS = 60000;
const CATALOG_ROLE_PROBE_RETRY_MS = 5000;
// Also the pool's acquireTimeoutMillis: generic-pool cannot time out a wait
// inside its create factory, so the slot wait needs its own bound.
const DRIVER_SLOT_WAIT_MS = 20000;
const driverSlots = createConnectionSlots(
  positiveIntegerFromEnv(process.env, 'SCOUT_CUBE_MAX_DRIVER_CONNECTIONS', 16),
  DRIVER_SLOT_WAIT_MS
);

// The limit hooks the driver's createConnection(); without it the cap would
// silently do nothing, so refuse to start instead.
if (typeof PostgresDriver.prototype.createConnection !== 'function') {
  throw new Error('PostgresDriver.createConnection is missing; the tenant connection limit would be inert');
}

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
    // release() is idempotent; a fatal socket error need not surface as 'end'.
    client.once('end', release);
    client.once('error', release);
    return client;
  }
}

function boundedPoolConfig(maxPoolSize) {
  return {
    maxPoolSize,
    idleTimeoutMillis: DRIVER_IDLE_TIMEOUT_MS,
    softIdleTimeoutMillis: DRIVER_IDLE_TIMEOUT_MS,
    evictionRunIntervalMillis: DRIVER_EVICTION_INTERVAL_MS,
    acquireTimeoutMillis: DRIVER_SLOT_WAIT_MS,
  };
}

const DEFAULT_DB_SSL_CA_FILE = '/cube/conf/rds-global-bundle.pem';
let dbSslCa = null;

function sslConfigForUrl(rawUrl) {
  if (!rawUrl) {
    return false;
  }
  const parsed = new URL(rawUrl);
  const host = parsed.hostname;
  if (!host || host === 'localhost' || host === '127.0.0.1' || host === 'platform-db') {
    return false;
  }
  // Read once and fail startup if missing: a remote database must never be
  // reached without verifying its certificate and hostname.
  if (dbSslCa === null) {
    const caFile = process.env.SCOUT_DB_SSL_CA_FILE || DEFAULT_DB_SSL_CA_FILE;
    const ca = readFileSync(caFile, 'utf8');
    // An empty ca makes Node fall back to its public roots instead of failing.
    if (!ca.includes('-----BEGIN CERTIFICATE-----')) {
      throw new Error(`Database CA file ${caFile} contains no certificates`);
    }
    dbSslCa = ca;
  }
  return { ca: dbSslCa, rejectUnauthorized: true };
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
// Pin search_path: the default "$user", public resolves differently for the
// owner and for the role, so the grant check and the reads could otherwise
// see different semantic_cubeschema tables.
// Not connectionString: pg lets URL parameters such as ?sslmode=no-verify
// replace the ssl option, which would bypass certificate verification.
const catalogPoolOptions = {
  ...connectionFromUrl(appDatabaseUrl),
  options: '-c search_path=public',
  connectionTimeoutMillis: CATALOG_QUERY_TIMEOUT_MS,
  statement_timeout: CATALOG_QUERY_TIMEOUT_MS,
  query_timeout: CATALOG_QUERY_TIMEOUT_MS,
};
// pg.Pool re-emits an idle client's error (e.g. RDS dropping an idle connection)
// on the pool, and an EventEmitter with no 'error' listener throws, killing Cube.
function catalogPoolWithErrorHandler(options) {
  const pool = new Pool(options);
  pool.on('error', (error) => {
    console.warn(`Idle Cube catalog connection failed and was discarded: ${error.message}`);
  });
  return pool;
}

const ownerPool = catalogPoolWithErrorHandler({ ...catalogPoolOptions, max: CATALOG_POOL_MAX });
let rolePool = null;
let roleProbe = null;
let nextRoleProbeAt = 0;

// The role is cluster-wide but its SELECT grant is per database, so check the
// grant in this database, not existence.
const ROLE_READY_SQL = `
  SELECT coalesce(
    pg_has_role(current_user, to_regrole($1)::oid, 'MEMBER')
      AND has_table_privilege(to_regrole($1)::oid, 'public.semantic_cubeschema', 'SELECT'),
    false
  ) AS ready
`;

// Cube deploys before the API applies migrations, so on the deploy that adds
// the grant it is missing for a few minutes. Until then, read as the owner, as
// before; once the grant exists every catalog read uses the role, and a later
// broken grant fails closed rather than falling back.
async function catalogPool() {
  if (rolePool) {
    return rolePool;
  }
  if (Date.now() >= nextRoleProbeAt) {
    roleProbe ??= ownerPool
      .query(ROLE_READY_SQL, [CATALOG_ROLE])
      .then(({ rows }) => {
        if (rows[0]?.ready) {
          rolePool ??= catalogPoolWithErrorHandler({
            ...catalogPoolOptions,
            max: CATALOG_POOL_MAX,
            options: `-c role=${CATALOG_ROLE} -c search_path=public -c default_transaction_read_only=on`,
          });
          return;
        }
        nextRoleProbeAt = Date.now() + CATALOG_ROLE_PROBE_INTERVAL_MS;
        console.warn(`Cube catalog role ${CATALOG_ROLE} cannot read semantic_cubeschema yet; reading it as the DATABASE_URL owner`);
      }, (error) => {
        nextRoleProbeAt = Date.now() + CATALOG_ROLE_PROBE_RETRY_MS;
        console.warn(`Cube catalog role check failed (${error.message}); reading semantic_cubeschema as the DATABASE_URL owner`);
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
        SELECT content_hash
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
    // Content only: re-promoting identical YAML must not recompile. Publication
    // freshness is fenced per query by queryRewrite's data revision, not here.
    return rows[0].content_hash;
  },

  scheduledRefreshTimer: false,
  scheduledRefreshContexts: () => [],
};
