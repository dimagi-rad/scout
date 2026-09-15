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

## Merge/deployment prerequisite: bounded result storage

**Do not deploy this change with the currently configured `memory` cache backend.**
Cube 1.6.39's `LocalCacheDriver` deletes expired entries only when that exact key is
read again; its cleanup method does not sweep them. Publication-specific result
keys are deliberately not reused, so old result data can otherwise accumulate
indefinitely. The query-cache front LRU does not bound these final result entries.
See the pinned [LocalCacheDriver implementation](https://github.com/cube-js/cube/blob/v1.6.39/packages/cubejs-query-orchestrator/src/orchestrator/LocalCacheDriver.ts).

The supported bounded-cache infrastructure prerequisite is tracked separately.
This change neither configures nor deploys that infrastructure. Redis is not a
supported query-cache backend in the pinned Cube version. A cache byte/key limit
must not be described as a bound on the entire service's memory: connection pools,
queues, compiler state, and storage-engine overhead are separate.

## Rolling compatibility

After the bounded-cache prerequisite is ready, deploy Cube before API/worker code.
Older API tokens need no new claim: Cube obtains the publication from the catalog.
Old published YAML still compiles on the new Cube; its next successful build
receives the SQL wrapper. The freshness fence applies once that wrapped YAML is
published. New wrapped YAML also compiles without the new context value because
`ARRAY[]::text[] IS NOT NULL` is a true predicate, permitting a safe rollback; the
publication freshness guarantee requires the new Cube configuration.

## Verification

- `node --test cube_config/cube.test.js` exercises request scoping, authoritative
  lookup/failure behavior, and stable orchestrator/driver identity without a DB.
- `tests/test_cube_publication_sql.py` covers generated wrapper contracts.
- `tests/test_semantic_query.py` verifies successful same-YAML publication and
  failed-build timestamp retention using an isolated ORM database.
- The opt-in `tests/smoke/test_cube_publication_cache.py` runs the pinned compiler,
  cache, and queue in a separate Node process inside a local Cube container. It
  uses an in-memory fake database driver and does not touch the serving process or
  any databases. Set `SCOUT_CUBE_COMPILER_CONTAINER` and select `-m smoke` to run
  physical, custom, and joined physical/custom cases. These tests prove the
  publication fence, not the unresolved memory backend's resource bound.
