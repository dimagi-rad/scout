const { Pool } = require('pg');

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
  if (!IDENTIFIER_RE.test(value || '')) {
    throw new Error(`Invalid ${label} in Cube security context`);
  }
  return value;
}

const appDatabaseUrl = process.env.DATABASE_URL || 'postgresql://platform:devpassword@platform-db:5432/agent_platform';
const managedDatabaseUrl = process.env.MANAGED_DATABASE_URL || appDatabaseUrl;
const appPool = new Pool({ connectionString: appDatabaseUrl, ssl: sslConfigForUrl(appDatabaseUrl) });
const managedConfig = connectionFromUrl(managedDatabaseUrl);

module.exports = {
  contextToAppId: ({ securityContext }) => {
    if (!securityContext?.workspaceId || !securityContext?.semanticModelId) {
      return 'scout_healthcheck';
    }
    const hash = securityContext.cubeSchemaHash || 'unknown';
    // schemaName must be part of the app id: the generated YAML is
    // schema-agnostic (tables resolve via the driver's search_path), so a
    // blue-green tenant-schema swap changes neither the YAML nor its hash.
    // Without schemaName here, Cube would keep serving queries through the
    // cached driver whose search_path still points at the old (dropped) schema.
    const schema = securityContext.schemaName || 'noschema';
    return `scout_${securityContext.workspaceId}_${securityContext.semanticModelId}_${hash}_${schema}`
      .replace(/[^a-zA-Z0-9_]/g, '_')
      .slice(0, 180);
  },

  dbType: () => 'postgres',

  driverFactory: ({ securityContext }) => {
    if (!securityContext?.schemaName || !securityContext?.readonlyRole) {
      return { type: 'postgres', ...managedConfig };
    }

    const schemaName = requireIdentifier(securityContext.schemaName, 'schemaName');
    const readonlyRole = requireIdentifier(securityContext.readonlyRole, 'readonlyRole');
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
