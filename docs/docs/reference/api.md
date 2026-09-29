# API reference

The API is used by Scout's own frontend. Authentication uses Django session
cookies; every mutating request needs the CSRF token (see
[Authentication](#authentication)).

Most endpoints are scoped to a workspace and live under
`/api/workspaces/<workspace_id>/`. The tables below abbreviate that prefix as
`…/`. Access is checked against the caller's workspace role: `read`,
`read_write` or `manage`. Unless a table says otherwise, any member can call an
endpoint.

## Authentication

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/auth/csrf/` | Sets the CSRF cookie and returns `{"csrfToken": "..."}`. |
| GET | `/api/auth/me/` | Current user. 401 if not signed in. |
| POST | `/api/auth/login/` | Email/password login. |
| POST | `/api/auth/logout/` | End the session. |
| GET | `/api/auth/providers/` | OAuth providers configured for this site, with connection status when signed in. |
| POST | `/api/auth/providers/<provider_id>/disconnect/` | Revoke the user's tokens for a provider. |
| GET | `/api/auth/tenants/` | The user's tenant memberships. |
| POST | `/api/auth/tenants/select/` | Mark a tenant as the active selection. |
| POST | `/api/auth/tenants/ensure/` | Find or create a tenant membership and select it. |
| GET, POST | `/api/auth/connections/` | List the user's connections, or add an API-key connection. |
| PATCH, DELETE | `/api/auth/connections/<connection_id>/` | Rotate a connection's API key, or remove the connection. |
| GET | `/api/auth/api-key-providers/` | Providers that accept API-key connections. |

Login takes `{"email": "...", "password": "..."}`. Login and `me` return:

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "user@example.com",
  "name": "Jane Doe",
  "is_staff": false,
  "onboarding_complete": true
}
```

`onboarding_complete` is true once the user has at least one active tenant
membership backed by a connection.

Login returns 400 for invalid JSON or missing fields, 401 for bad credentials,
and 429 once an email has 5 failed logins within 5 minutes.

There is no sign-up endpoint. Accounts are created by the first OAuth login or
by an administrator.

### OAuth

OAuth login is handled by django-allauth under `/accounts/`. Only the
per-provider `/accounts/<provider>/login/` and `/accounts/<provider>/login/callback/`
routes are mounted, plus `/accounts/login/cancelled/` and `/accounts/login/error/`.
Which providers appear depends on the social apps configured for the site; the
frontend reads them from `/api/auth/providers/`.

## Workspaces

| Method | Path | Role | Description |
|--------|------|------|-------------|
| GET | `/api/workspaces/` | — | Workspaces the user belongs to. |
| POST | `/api/workspaces/` | — | Create a workspace from `name` and `tenant_ids` (at least one; a workspace never exists without a source). The creator becomes its manager. |
| GET | `…/` | read | Workspace detail. |
| PATCH | `…/` | manage | Rename (`name`), or set the agent's `system_prompt` (max 10,000 characters). |
| DELETE | `…/` | manage | Delete. |
| GET | `…/members/` | read | Members and live invites. |
| POST | `…/members/` | manage | Add a member by `email` and `role`. Creates an invite if the user has no account or can't yet use every tenant. |
| PATCH | `…/members/<membership_id>/` | manage | Change a member's role. |
| DELETE | `…/members/<membership_id>/` | manage | Remove a member. Members can also remove themselves. |
| PATCH, DELETE | `…/invites/<invite_id>/` | manage | Change an invite's role, or revoke it. |
| GET | `/api/invites/` | — | The signed-in user's invites that are waiting on tenant access. |
| GET, POST | `…/tenants/` | read / manage | List the workspace's tenants, or add one (`tenant_id`). |
| DELETE | `…/tenants/<workspace_tenant_id>/` | manage | Remove a tenant. Removing the last one deletes the whole workspace: without `?confirm_delete_workspace=true` it answers 409 with `requires_confirmation: "delete_workspace"`, `workspace_name` and `member_count` and changes nothing; with it, the workspace is deleted under the same refusals as `DELETE …/` and it answers 200 `{"workspace_deleted": true}`. |
| POST | `…/access/verify/` | — | Re-check the caller's access to the workspace's tenants upstream. |

## Chat

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat/` | Send a message; the response streams. |
| GET | `…/threads/` | The user's threads in the workspace. |
| GET, PATCH | `…/threads/<thread_id>/` | Thread summary, or rename it (`title`). |
| GET | `…/threads/<thread_id>/messages/` | Messages for a thread. |
| GET | `…/threads/<thread_id>/artifacts/` | Artifacts linked to a thread. |
| POST | `…/threads/<thread_id>/viewed/` | Mark the thread as viewed. |

Threads are private to the user who created them. They cannot be shared.

### Chat request

```json
POST /api/chat/
{
  "messages": [
    {"role": "user", "content": "How many visits were recorded last month?"}
  ],
  "data": {
    "workspaceId": "550e8400-...",
    "threadId": "optional-thread-uuid"
  }
}
```

- `messages`: only the last message is read. Its text comes from `content`, or
  from its `text` parts (AI SDK v6 format). It is limited to 10,000 characters.
- `workspaceId`: required. Accepted in `data` or at the top level.
- `threadId`: optional. A new UUID is generated if omitted.

The response is a `text/event-stream` in the Vercel AI SDK v6 UI message
stream format. See [Response processing](agent.md#response-processing) for the
chunk types.

Errors:

- `400`: invalid JSON, no messages, no `workspaceId`, an empty message, or a
  message over the length limit.
- `401`: not signed in.
- `403`: no access to the workspace.
- `404`: the thread belongs to another user or workspace.
- `405`: not a POST.
- `409`: a background response is still being written to this thread.
- `429`: more than 20 messages in 60 seconds from this user. The response
  carries `Retry-After` and `X-RateLimit-*` headers.
- `500`: the agent could not be initialized. The message includes a reference
  for the logs.

## Artifacts

| Method | Path | Role | Description |
|--------|------|------|-------------|
| GET | `…/artifacts/` | read | Latest version of each artifact. Accepts `search`. |
| PATCH | `…/artifacts/<artifact_id>/` | read_write | Update title or description. |
| DELETE | `…/artifacts/<artifact_id>/` | read_write | Soft-delete. |
| POST | `…/artifacts/<artifact_id>/undelete/` | read_write | Restore a soft-deleted artifact. |
| GET | `…/artifacts/<artifact_id>/data/` | read | Artifact code, data and query metadata as JSON. |
| GET, POST | `…/artifacts/<artifact_id>/query-data/` | read | Run the artifact's semantic queries. POST takes runtime values such as date-filter selections. |
| GET | `…/artifacts/<artifact_id>/semantic-queries/` | read | The artifact's semantic queries, paginated with `limit` (default 25, max 100) and `offset`. |
| GET, POST | `…/artifacts/<artifact_id>/recovery/` | read / read_write | Inspect the data behind an artifact, or start repairing it. |
| GET | `…/artifacts/<artifact_id>/sandbox/` | read | HTML page that renders a non-story artifact in a sandboxed iframe. |
| GET | `…/artifacts/<artifact_id>/export/<format>/` | read | Download as `html`. Other formats return 400. |

Artifacts have no public share links.

## Recipes

| Method | Path | Role | Description |
|--------|------|------|-------------|
| GET | `…/recipes/` | read | Recipes in the workspace. |
| GET | `…/recipes/<recipe_id>/` | read | Recipe detail. |
| PUT | `…/recipes/<recipe_id>/` | read_write | Update a recipe. |
| DELETE | `…/recipes/<recipe_id>/` | read_write | Soft-delete a recipe. There is no undelete endpoint. |
| POST | `…/recipes/<recipe_id>/run/` | read | Start a run with `variable_values`. Returns 202 with the pending run; the run executes in the background worker. |
| GET | `…/recipes/<recipe_id>/runs/` | read | Runs for a recipe, newest first. |

## Knowledge

| Method | Path | Role | Description |
|--------|------|------|-------------|
| GET | `…/knowledge/` | read | List knowledge entries and agent learnings. |
| POST | `…/knowledge/` | read_write | Create a knowledge entry. Learnings can't be created here. |
| GET | `…/knowledge/<item_id>/` | read | One entry or learning. |
| PUT | `…/knowledge/<item_id>/` | read_write | Partial update of an entry or learning. |
| DELETE | `…/knowledge/<item_id>/` | read_write | Delete an entry or learning. |
| GET | `…/knowledge/export/` | read | Entries as a zip of markdown files with YAML frontmatter. |
| POST | `…/knowledge/import/` | read_write | Import a zip of markdown files. |

### List parameters

| Parameter | Description |
|-----------|-------------|
| `type` | `entry` or `learning`. Both when omitted. |
| `search` | Case-insensitive match on entry title/content, or learning description, original error and SQL. |
| `page` | Page number (default 1). |
| `page_size` | Items per page (default 50, max 200). |

The response has `results` (entries and learnings merged, newest first; each
item has a `type` field) and `pagination` (`page`, `page_size`, `total_count`,
`total_pages`, `has_next`, `has_previous`).

### Create an entry

```json
POST …/knowledge/
{
  "type": "entry",
  "title": "Active user",
  "content": "A user with at least one form submission in the last 30 days.",
  "tags": ["metric"]
}
```

### Import

`POST …/knowledge/import/` takes `multipart/form-data` with the zip in a `file`
field, up to 25 MB decompressed. Each `.md` file needs a `title` in its YAML
frontmatter; files without one are skipped. An entry whose title matches an
existing entry updates it in place. The response counts `created`, `updated`
and `skipped` entries and lists per-file `errors`. It is 207 when any file
failed, and the whole import rolls back on a database error.

## Data dictionary and semantic model

| Method | Path | Role | Description |
|--------|------|------|-------------|
| GET | `…/data-dictionary/` | read | Tables and columns in the workspace's data. |
| GET | `…/data-dictionary/tables/<qualified_name>/` | read | One table, with its annotations. |
| PUT | `…/data-dictionary/tables/<qualified_name>/` | read_write | Save table annotations (TableKnowledge). |
| GET | `…/datasets/` | read | Semantic datasets. |
| GET | `…/datasets/<dataset_name>/` | read | One dataset's members. |
| POST | `…/semantic-query/` | read | Run a structured semantic query. |
| GET | `…/threads/<thread_id>/canvas/` | read | The thread's Semantic Canvas. |
| POST | `…/threads/<thread_id>/canvas/apply/` | read_write | Apply changes to the canvas draft. |
| POST | `…/threads/<thread_id>/canvas/commit/` | read_write | Commit the canvas to the workspace's semantic model. |

## Data loading

| Method | Path | Role | Description |
|--------|------|------|-------------|
| POST | `…/refresh/` | read_write | Start a data refresh. |
| GET | `…/refresh/status/` | read | Refresh progress. |
| POST | `…/materialization/cancel/` | read_write | Cancel a running materialization. |
| POST | `…/materialize/retry/` | read_write | Retry a failed materialization. |
| GET | `…/jobs/active/` | read | The workspace's active background jobs. |
| POST | `…/jobs/<thread_job_id>/cancel/` | read_write | Cancel a background job. |

## Transformations

`/api/transformations/` is a DRF router over the user's tenant and workspace
transformation assets and runs:

| Method | Path | Description |
|--------|------|-------------|
| GET, POST | `/api/transformations/assets/` | List or create assets. |
| GET, PUT, PATCH, DELETE | `/api/transformations/assets/<id>/` | One asset. System assets are read-only. |
| GET | `/api/transformations/assets/<id>/lineage/` | The asset's lineage chain. |
| GET | `/api/transformations/runs/` | Run history. Accepts `tenant_id`. |
| GET | `/api/transformations/runs/<id>/` | One run. |
| POST | `/api/transformations/runs/trigger/` | Run the pipeline for `tenant_id` (and optional `workspace_id`) synchronously. |

## Other endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health/` | Readiness check. Probes the database and the task queue. Returns 200 with `{"status": "ok", "checks": {...}}`, or 503 with `"status": "unhealthy"`. No authentication. |
| GET | `/widget.js` | Embeddable widget script. |
| — | `/admin/` | Django admin. |
