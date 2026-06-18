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
 *
 * The data model files (Task 7) reference the active schema via:
 *   sql_table: `{COMPILE_CONTEXT.security_context.schema_name}.stg_visits`
 *
 * Note: the checkSqlAuth signature in current Cube docs is (req, username, password)
 * — first argument is the request object, NOT the SQL query string.
 * The brief showed (query, username, password) which is incorrect for current Cube.
 */

const jwt = require("jsonwebtoken");

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
      decoded = jwt.verify(password, secret);
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
};
