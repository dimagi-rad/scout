# Cube Core — Scout semantic layer

Scout uses [Cube Core](https://cube.dev/) as a multi-tenant semantic layer over the managed
PostgreSQL database. Cube exposes two APIs:

- **REST API / Playground** on `:4000`
- **SQL API (pg-wire)** on `:15432` — used by the Scout agent and BI tools

## Running

```bash
# Start Cube alongside other dev services
docker compose up cube

# Or start everything at once
docker compose up
```

The REST playground is available at `http://localhost:4000` in dev mode
(`CUBEJS_DEV_MODE=true`).

## Multi-tenant architecture

Scout operates on a per-workspace schema model. Each workspace maps to exactly one
PostgreSQL schema in the managed database:

| Workspace type | Schema pattern | Example |
|---|---|---|
| Single-tenant | `t_<tenant_id>` | `t_42` |
| Multi-tenant (view schema) | `ws_<hash>` | `ws_a1b2c3` |

### JWT securityContext shape

A later task (Task 8) will mint a short-lived JWT signed with `CUBEJS_API_SECRET`
and pass it as the **SQL-API password** when connecting via pg-wire. The JWT payload
must carry:

```json
{
  "workspace_id": "<uuid>",
  "schema_name": "t_42"
}
```

`cube.js` verifies this JWT in `checkSqlAuth` and surfaces the payload as the
request's `securityContext`.

### COMPILE_CONTEXT schema selection

Data model files reference the per-tenant schema via `COMPILE_CONTEXT`:

```yaml
cubes:
  - name: visits
    sql_table: "{COMPILE_CONTEXT.security_context.schema_name}.stg_visits"
```

`contextToAppId` and `contextToOrchestratorId` both key on `schema_name`, so
Cube compiles and caches a separate model per tenant schema.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `CUBEJS_API_SECRET` | **Yes** | JWT signing secret — **MUST equal the secret Scout uses to sign workspace JWTs** (Task 8) |
| `CUBEJS_DB_TYPE` | Yes | `postgres` |
| `CUBEJS_DB_HOST` | Yes | Managed DB host (dev: `platform-db`) |
| `CUBEJS_DB_PORT` | Yes | Managed DB port (default `5432`) |
| `CUBEJS_DB_NAME` | Yes | Managed DB name (dev: `agent_platform`) |
| `CUBEJS_DB_USER` | Yes | Managed DB user |
| `CUBEJS_DB_PASS` | Yes | Managed DB password |
| `CUBEJS_PG_SQL_PORT` | Yes | Port for the pg-wire SQL API (default `15432`) |
| `CUBEJS_DEV_MODE` | No | Set to `true` in dev to enable the Playground UI |
| `CUBE_URL` | No | Base URL for smoke tests (e.g. `http://localhost:4000`) |

### CUBEJS_API_SECRET

This secret is the single shared secret between Scout (which mints the JWTs) and
Cube (which verifies them). In production, generate a strong random value and keep
it consistent across both services:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

## Managed database

Cube connects to the **managed database** — the same PostgreSQL instance where
Scout's tenant schemas (`t_<id>`, `ws_<hash>`) live. In dev this is the same
`platform-db` container (database `agent_platform`). In production,
`MANAGED_DATABASE_URL` may point to a separate managed-data Postgres instance;
set the `CUBEJS_DB_*` variables to match that instance's credentials.

## Model files

Generated data model YAML files live in `cube/model/<schema_name>/` — one
subdirectory per workspace schema. Each subdirectory contains one YAML file
per cube plus a `views.yml` for any views:

```
cube/model/
  t_42/
    visits.yml       # stg_visits cube with dimensions, measures, joins
    flws.yml         # raw_users cube
    views.yml        # program_health view
  ws_a1b2c3/
    visits.yml
    flws.yml
    views.yml
```

The Task 7 generator (`apps/transformations/services/cube_model_generator.py`)
writes these files after generating the model from:
- The staged schema column list (`stg_visits`, `raw_users`, etc.)
- Connect form definitions (question labels become dimension titles)
- Business knowledge / KPI metric definitions
- Relationship declarations (used as Cube joins)

`cube.js` picks up the right directory via `repositoryFactory`:

```javascript
repositoryFactory: ({ securityContext }) =>
  new FileRepository(`model/${securityContext.schema_name}`),
```

A workspace whose model directory is empty or missing gets an empty model —
Cube returns an empty schema for that tenant instead of erroring out.
