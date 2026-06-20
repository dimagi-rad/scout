/**
 * Cube Core multi-tenant configuration for Scout.
 *
 * Multi-tenant model
 * ------------------
 * Scout (Task 8) resolves workspace_id -> schema_name and mints a JWT signed
 * with CUBEJS_API_SECRET containing:
 *   { workspace_id: "<uuid>", schema_name: "t_<id>" | "ws_<hash>" }
 *
 * That JWT is passed as the SQL-API password when connecting via pg-wire.
 * This file:
 *   1. Verifies the JWT in checkSqlAuth and surfaces its payload as securityContext.
 *   2. Keys compilation and orchestrator cache per schema_name so each tenant
 *      gets its own data model compile context.
 *   3. Loads per-workspace model files from cube/model/<schema_name>/ via
 *      repositoryFactory so each tenant gets its own generated data model.
 *
 * The data model files (Task 7) reference the active schema via:
 *   sql_table: `{COMPILE_CONTEXT.security_context.schema_name}.stg_visits`
 *
 * Per-workspace model layout
 * --------------------------
 * The Task 7 generator writes YAML to cube/model/<schema_name>/:
 *
 *   cube/model/
 *     t_42/
 *       visits.yml
 *       flws.yml
 *       views.yml
 *     ws_a1b2c3/
 *       visits.yml
 *       ...
 *
 * A workspace whose model directory is empty or missing gets an empty model
 * (no cubes, no views). That degrades gracefully — Cube will not error on
 * SELECT 1 or meta requests; it simply returns an empty schema for that tenant.
 *
 * Note: the checkSqlAuth signature in current Cube docs is (req, username, password)
 * — first argument is the request object, NOT the SQL query string.
 * The brief showed (query, username, password) which is incorrect for current Cube.
 */

const jwt = require("jsonwebtoken");
const { FileRepository } = require("@cubejs-backend/server-core");
const { PostgresDriver } = require("@cubejs-backend/postgres-driver");

module.exports = {
  /**
   * Verify the JWT Scout passes as the SQL-API password.
   * Returns the securityContext extracted from the token payload.
   *
   * @param {object} req - Cube request object (not used here)
   * @param {string} username - SQL username (ignored; identity is in the JWT)
   * @param {string} password - Signed JWT minted by Scout
   */
  checkSqlAuth: (req, username, password) => {
    const secret = process.env.CUBEJS_API_SECRET;
    if (!secret) {
      throw new Error("CUBEJS_API_SECRET is not configured");
    }

    let decoded;
    try {
      decoded = jwt.verify(password, secret, { algorithms: ["HS256"] });
    } catch (err) {
      throw new Error(`SQL API authentication failed: ${err.message}`);
    }

    const { workspace_id, schema_name } = decoded;
    if (!workspace_id || !schema_name) {
      throw new Error(
        "JWT is missing required claims: workspace_id and schema_name"
      );
    }

    return {
      password,
      securityContext: {
        workspace_id,
        schema_name,
      },
    };
  },

  /**
   * Connect to Postgres under the workspace's least-privilege read-only role
   * instead of the base superuser — DB-layer tenant isolation (reviewer #302,
   * design §7). `provision_workspace_ro_role` creates `<schema_name>_ro` with
   * USAGE + SELECT on ONLY that workspace's constituent schemas; pinning the
   * connection's session role to it (libpq `-c role=...`, applied at connect
   * for every connection in this tenant's pool) means a query for one workspace
   * can never read another's data — even via a misconfigured model or an
   * injected column expression. The pool is already per-tenant
   * (contextToOrchestratorId is keyed on schema_name), so each pool is pinned
   * to exactly one role.
   *
   * Scope: cross-opp workspaces (`ws_<hash>` schemas) have a provisioned role,
   * so they are pinned. Single-tenant (`t_<id>`) and anonymous (dev Playground)
   * contexts keep the base connection — no `_ro` role is provisioned for those
   * yet, and pinning to a non-existent role would refuse the connection.
   *
   * @param {{ securityContext?: { schema_name?: string } }} ctx
   */
  driverFactory: ({ securityContext }) => {
    const base = {
      host: process.env.CUBEJS_DB_HOST,
      port: process.env.CUBEJS_DB_PORT,
      database: process.env.CUBEJS_DB_NAME,
      user: process.env.CUBEJS_DB_USER,
      password: process.env.CUBEJS_DB_PASS,
    };
    const schema = securityContext && securityContext.schema_name;
    if (schema && schema.startsWith("ws_")) {
      // Pin every connection in this workspace's pool to its read-only role.
      return new PostgresDriver({ ...base, options: `-c role=${schema}_ro` });
    }
    return new PostgresDriver(base);
  },

  /**
   * Isolate the compiled data model per tenant schema.
   * Cube caches one compiled model per appId — using schema_name ensures
   * each tenant resolves COMPILE_CONTEXT.security_context.schema_name correctly.
   *
   * NOTE: securityContext can be undefined for unauthenticated requests (the
   * dev-mode Playground) and for background work that carries no context.
   * Guard against it so Cube returns an empty model instead of crashing with
   * "Cannot read properties of undefined (reading 'schema_name')".
   */
  contextToAppId: ({ securityContext }) =>
    `SCOUT_${(securityContext && securityContext.schema_name) || "anonymous"}`,

  /**
   * Isolate the query orchestrator (pre-aggregation + cache) per tenant schema.
   */
  contextToOrchestratorId: ({ securityContext }) =>
    `SCOUT_${(securityContext && securityContext.schema_name) || "anonymous"}`,

  /**
   * Load per-workspace model files from cube/model/<schema_name>/.
   *
   * The Task 7 generator writes YAML files here after generating the model.
   * If the directory is missing or empty (or securityContext is absent),
   * FileRepository returns an empty repository — Cube compiles an empty model,
   * which is safe and returns an empty schema rather than erroring out.
   *
   * @param {{ securityContext?: { schema_name?: string } }} ctx
   * @returns {FileRepository}
   */
  repositoryFactory: ({ securityContext }) =>
    new FileRepository(
      `model/${(securityContext && securityContext.schema_name) || "__anonymous__"}`
    ),

  /**
   * No predefined background-refresh contexts: Scout builds pre-aggregations
   * lazily on query (with a valid securityContext). Returning [] avoids Cube's
   * scheduled refresh running contextToAppId with an undefined securityContext.
   * For production pre-agg warmup, populate this with the active workspaces'
   * { securityContext: { workspace_id, schema_name } } entries.
   */
  scheduledRefreshContexts: () => [],
};
