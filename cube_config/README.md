# Cube publication cache identity

A successful semantic-schema publication updates the active `CubeSchema.updated_at`,
even when its YAML/content hash is unchanged. A failed build leaves that published
timestamp intact. Generated physical and custom dataset SQL bind the timestamp as
a no-op PostgreSQL predicate. Cube includes SQL parameters in both result-cache
and query-queue keys, so an older queued query cannot populate a newer publication's
result entry.

`queryRewrite` reads the active timestamp from the application database, scoped to
the authenticated workspace and semantic model. It overrides any token claim and
preserves microseconds. Concurrent queries in one REST request share the lookup;
different requests do not. Missing active schemas or catalog failures fail closed.
The extra catalog roundtrip uses the existing shared pool (default maximum ten
connections), with five-second acquisition, client-query, and server-statement
timeouts. It does not allocate an orchestrator, driver, or connection pool per
publication; existing workspace/schema/read-only-role isolation remains unchanged.

## Bounded query-result retention without CubeStore

Scout continues to use one Cube process per environment with the `memory` cache
and queue backend. This fix adds no CubeStore or Redis service, credential, or
per-publication orchestrator/connection pool. Scout's existing Redis use elsewhere
is unchanged.

The upstream Cube 1.6.39 `LocalCacheDriver` deletes expired entries only when the
same key is read again; its cleanup method does not sweep them. Publication keys
are deliberately not reused, so the unpatched driver retains obsolete results.
The separate front LRU does not bound those final entries. See the pinned
[upstream implementation](https://github.com/cube-js/cube/blob/v1.6.39/packages/cubejs-query-orchestrator/src/orchestrator/LocalCacheDriver.ts).

The Cube image applies a small **maintained dependency patch at build time**;
this is not a public Cube configuration extension or a runtime monkey-patch.
The patch checks the installed package version and exact upstream module hash,
then installs Scout's bounded implementation. A different Cube version or changed
upstream module must fail verification rather than silently boot unpatched.

- Only keys in the `#SQL_QUERY_RESULT:` namespace share the process-wide LRU
  entry and serialized-payload-plus-UTF-8-key byte budgets. This includes cached
  refresh-key results. Least-recently-used results are discarded under pressure;
  a miss runs the query again. An oversized result is rejected with a clear
  size-limit error asking the caller to reduce rows or columns. Only a compact
  terminal error marker is retained, within the same budgets, for at most 30
  seconds. This prevents retries from endlessly rerunning a result that cannot
  fit; the oversized rows are never retained in this store.
- One unreferenced sweep timer expires abandoned keys without another read.
  There is no timer per write. Reads never serve an already expired entry.
- Live coordination metadata is not LRU-evicted: Cube uses some of those markers
  to protect in-use pre-aggregation tables. Lock leases are separate from result
  pressure and have bounded admission. Releasing an expired lease cannot release
  a successor's lock. Cancellation and retained-lease behavior remain covered.
- Shared-driver cleanup removes only expired state, not another workspace's
  still-valid values or locks. Global reset behavior remains explicit.

Validated integer settings (defaults require no deployment overrides):

| Environment variable | Default | Scope |
|---|---|---|
| `SCOUT_CUBE_CACHE_MAX_RESULT_ENTRIES` | 4096 | Retained query-result keys across the process |
| `SCOUT_CUBE_CACHE_MAX_RESULT_BYTES` | 67108864 (64 MiB) | Serialized result payload plus UTF-8 key bytes |
| `SCOUT_CUBE_CACHE_MAX_LOCKS` | 1024 | Concurrent retained lock leases; full admission fails closed |
| `SCOUT_CUBE_CACHE_SWEEP_INTERVAL_MS` | 15000 | Single idle-expiration sweep interval |

All settings must be positive safe integers. The result-byte budget must be at
least 1024 bytes so a terminal marker fits. Result keys are limited to 512 UTF-8
bytes; Cube's hashed query-result keys fit within this bound. The sweep interval
must fit Node's signed 32-bit timer range. Invalid settings fail startup.

These are **not a cap on total Node memory**. JavaScript object overhead, live
queries, query queues, non-result metadata, compiler state and connection pools
are separate. Eviction trades cache hit rate for bounded retained result data;
it does not alter authorization or source rows. Queries too large to retain
must be narrowed instead of receiving partial or silently truncated data.

### Cube upgrades

Keep the Docker image version, upstream module hash, patch and actual-package
tests together. On a Cube upgrade, re-audit the cache/queue call sites and lock/
metadata contracts before updating the expected hash; do not simply disable the
guard. Remove the patch if upstream provides equivalent tested expiration and
retention bounds. The dependency remains licensed under its upstream terms.

## Rolling compatibility

Deploy the verified patched Cube image before API/worker code.
Older API tokens need no new claim: Cube obtains the publication from the catalog.
Old published YAML still compiles on the new Cube; its next successful build
receives the SQL wrapper. The freshness fence applies once that wrapped YAML is
published. New wrapped YAML also compiles without the new context value because
`ARRAY[]::text[] IS NOT NULL` is a true predicate, permitting a safe rollback; the
publication freshness guarantee requires the new Cube configuration.

## Verification

- `node --test cube_config/*.test.js` exercises bounded retention, lock and
  metadata safety, idle expiration, patch integrity, request scoping, authoritative
  lookup/failure behavior, and stable orchestrator/driver identity without a DB.
- `tests/test_cube_publication_sql.py` covers generated wrapper contracts.
- `tests/test_semantic_query.py` verifies successful same-YAML publication and
  failed-build timestamp retention using an isolated ORM database.
- The opt-in `tests/smoke/test_cube_publication_cache.py` runs the pinned compiler,
  cache, and queue in a separate Node process inside a local Cube container. It
  uses an in-memory fake database driver and does not touch the serving process or
  any databases. Set `SCOUT_CUBE_COMPILER_CONTAINER` and select `-m smoke` to run
  physical, custom, and joined physical/custom cases. A further actual-package
  case verifies 100 publications under small result-entry/byte budgets, count
  and byte-pressure eviction, explicit oversized-result errors, idle expiry and
  protected metadata/locks. An actual OrchestratorApi test also covers slow
  query polling: normal results complete, and oversized results terminate with
  an actionable error instead of repeatedly returning `Continue wait`. CI builds
  this exact Dockerfile and runs these cases in a network-disabled container, so
  a green unit suite cannot conceal an unpatched deployed dependency.
