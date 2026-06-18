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
   * Isolate the compiled data model per tenant schema.
   * Cube caches one compiled model per appId — using schema_name ensures
   * each tenant resolves COMPILE_CONTEXT.security_context.schema_name correctly.
   */
  contextToAppId: ({ securityContext }) =>
    `SCOUT_${securityContext.schema_name}`,

  /**
   * Isolate the query orchestrator (pre-aggregation + cache) per tenant schema.
   */
  contextToOrchestratorId: ({ securityContext }) =>
    `SCOUT_${securityContext.schema_name}`,

  /**
   * Load per-workspace model files from cube/model/<schema_name>/.
   *
   * The Task 7 generator writes YAML files here after generating the model.
   * If the directory is missing or empty, FileRepository returns an empty
   * repository — Cube compiles an empty model, which is safe and returns
   * an empty schema rather than erroring out.
   *
   * @param {{ securityContext: { schema_name: string } }} ctx
   * @returns {FileRepository}
   */
  repositoryFactory: ({ securityContext }) =>
    new FileRepository(`model/${securityContext.schema_name}`),
};
