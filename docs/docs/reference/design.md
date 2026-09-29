# Core design

How Scout's data model and access rules work today: tenants, workspaces,
roles, invitations, threads, artifacts, recipes and knowledge. Users reach data
through workspaces, which sit on top of one or more tenants. They work in a
chat interface backed by an AI agent, and can save artifacts and recipes from
their analyses.

---

## Tenants

A tenant is a scoped source of data in an external system, identified by its
provider and external ID. Scout supports three providers:

| Provider | Tenant |
|----------|--------|
| `commcare` (CommCare HQ) | A project space (domain) |
| `commcare_connect` (CommCare Connect) | An opportunity |
| `ocs` (Open Chat Studio) | A chatbot |

### Tenant memberships

A user's access to a tenant is a `TenantMembership`, backed by a connection:
an OAuth sign-in, or an API key for CommCare and Open Chat Studio. Scout
discovers the user's tenants from the provider and creates or updates tenants
and memberships:

- when the user signs in with OAuth or connects a new OAuth account;
- when the user adds an API-key connection;
- while onboarding is incomplete, when the frontend polls `/api/auth/me/`;
- for an invitee or existing members, when a manager invites a user or adds a
  tenant to a workspace.

Tenants the provider no longer lists have their membership archived rather than
deleted. Disconnecting a provider or removing a connection also archives its
memberships.

### Data storage

- Each tenant's data is loaded into its own PostgreSQL schema. The schema name
  includes a digest of the provider and external ID, so two tenants can't share
  a schema.
- Users with access to the same tenant share its schema.
- Each schema has a read-only role that all agent and semantic queries run
  under.
- Metadata about the tenant (such as CommCare case types and form structure)
  and the history of data loads are kept in Scout's own database, and survive
  the schema being dropped.

### Loading data

Data is loaded by a materialization pipeline defined per provider in
`pipelines/*.yml`. A run has three phases: discover (fetch metadata), load
(page through the provider's API into `raw_*` tables), and transform (dbt
models from transformation assets). Each run is recorded as a
`MaterializationRun`.

A load starts when:

- the agent runs `run_materialization`, which needs the read-write role
  (see [Agent architecture](agent.md#roles-and-run-modes));
- a member with the read-write role refreshes the data, retries a failed load,
  or repairs an artifact's data.

There is no scheduled refresh.

### Refresh

A refresh loads every tenant in the workspace into a new candidate schema while
the current schema keeps serving queries. When a tenant's load succeeds, the
candidate becomes the active schema and the old one is retired 30 minutes
later, once no workspace views depend on it. Workspace views and semantic
models that depend on the tenant are rebuilt. If the load fails, the candidate
is dropped and the old schema keeps serving.

A refresh for a tenant that already has one in progress is refused with 409.
Loads of the same tenant never run concurrently.

### Schema expiry

- A schema that hasn't been accessed for 24 hours (`SCHEMA_TTL_HOURS`) is
  dropped by a sweep that runs every 30 minutes. Schemas still loading are
  never expired.
- Sending a chat message or starting a recipe run marks the workspace's view
  schema and all its tenant schemas as accessed. Every agent data-tool call also
  marks the schema it reads.
- Each schema expires independently. Dropping a workspace view schema doesn't
  drop the tenant schemas under it. A tenant schema isn't dropped while a
  workspace view still depends on it: those workspaces are asked to rebuild,
  and the drop is retried.
- After a schema is dropped, queries fail until the data is loaded again. The
  agent can start that load (see [Loading data](#loading-data)), and a chat
  resumes when it finishes.

---

## Workspaces

A workspace is a layer on top of one or more tenants and is the main interface
to Scout. It holds threads, artifacts, recipes, knowledge (TableKnowledge,
KnowledgeEntry, AgentLearning), its semantic model, and a custom
`system_prompt` for the agent.

### Auto-created workspaces

When a tenant membership is created, Scout creates a single-tenant workspace
for that user and tenant, with the user as manager, unless the user already has
one. Each user gets their own auto-created workspace; they are not shared.

### Creating and deleting

- Any user can create a workspace from tenants they have memberships for, and
  becomes its manager. They must be able to use every selected tenant.
- Managers can rename a workspace and edit its system prompt (up to 10,000
  characters).
- Only managers can delete a workspace. Deletion is refused if it is the
  requester's last workspace covering one of its tenants. It removes the
  workspace's threads, artifacts,
  recipes, knowledge and pending invites.

### Workspace access

Access to a workspace requires a workspace membership and a usable credential
for its tenants. With `WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT` off (the
default), one covered tenant is enough. With it on, the member must be able to
use every tenant in the workspace.

A member who loses a tenant is denied access, with a message naming each
missing source and how to fix it. Their membership is kept, and access returns
automatically once the credential is usable again. While denied, a member can
still leave, hand the manager role to someone else, remove the missing source
(managers), or delete the workspace if no one else is in it.

With `UPSTREAM_ACCESS_FRESHNESS_ENFORCED` on (off by default), access also
needs proof from the provider, less than 5 minutes old, that the user still has
the tenant. A stale proof is rechecked on the next request. If the provider
confirms the user has lost access, the tenant membership is archived. If the
provider can't be reached, the request is denied as retryable and nothing is
archived. `POST /api/workspaces/<id>/access/verify/` lets a user retry the
check.

---

## Roles and permissions

Each workspace member has one role.

| Action | read | read_write | manage |
|--------|:----:|:----------:|:------:|
| View threads they own, artifacts, recipes, runs and knowledge | yes | yes | yes |
| Chat with the agent | yes | yes | yes |
| Run recipes | yes | yes | yes |
| Agent write tools (artifacts, recipes, learnings, materialization) | no | yes | yes |
| Share their own threads | no | yes | yes |
| Edit or soft-delete artifacts and recipes | no | yes | yes |
| Create and edit knowledge entries, edit learnings, annotate tables | no | yes | yes |
| Refresh data, retry or cancel loads, repair artifact data | no | yes | yes |
| Edit the semantic model through the Semantic Canvas | no | yes | yes |
| Add, remove and change the roles of members; manage invites | no | no | yes |
| Rename, edit the system prompt, delete the workspace | no | no | yes |
| Add or remove tenants | no | no | yes |

For read members, the agent is built without write tools (see
[Agent architecture](agent.md#roles-and-run-modes)). A recipe run by a read
member runs with that member's role.

- Managers can assign any role, including manage.
- The last manager can't be demoted or removed, including by leaving.
- Any member can leave a workspace.
- Removing a member deletes their threads in the workspace. Their artifacts and
  recipes stay.
- There is no owner role and no superuser bypass. Superusers need a membership
  like anyone else; separately, they can use the Django admin.

---

## Invitations

A manager adds someone by email and role (`POST …/members/`). The outcome
depends on the person:

- **No Scout account**: a `pending` invite is created and an email is sent. It
  is accepted automatically when they sign in with OAuth, if the invite matches
  one of their verified email addresses or their account email, and they can
  use the workspace's tenants.
- **Existing account that can use every tenant**: they become a member
  straight away. Scout re-resolves their tenant memberships with their own
  credentials first.
- **Existing account missing a tenant**: an `awaiting_access` invite is created
  and the person is emailed, naming the missing sources. It resolves at a later
  sign-in once they have access. Users see their waiting invites through
  `GET /api/invites/`.

Invites expire after 30 days. There is at most one live invite per workspace
and email; inviting again updates it. Managers can change an invite's role or
revoke it. Accepting an invite never changes the role of an existing member.

---

## Threads

A thread is a chat session between a user and the agent, owned by that user
and tied to one workspace. Threads are private: other members can't list or
read them, and there is no way to share one.

- Threads are deleted when their owner is removed from the workspace, when the
  workspace is deleted, or when the user is deleted.

---

## Artifacts

Artifacts are saved outputs, usually story dashboards created by the agent
(see [Artifact types](artifact-types.md)).

- All members can view artifacts. Their live semantic queries run against the
  workspace's current data whenever the artifact is opened, with results cached
  briefly. Opening an artifact doesn't reload data from the provider.
- Only the agent creates artifacts, and only for members with the read-write
  role. Read-write members can edit titles and descriptions, soft-delete and
  undelete through the API.
- Soft-deleted artifacts are hidden from every list, including thread artifact
  lists. With `--confirm`, the `purge_deleted_artifacts` management command
  permanently deletes artifacts soft-deleted more than 30 days ago (without it,
  it only reports them). It is not scheduled.
- Artifacts have no share links.
- If an artifact's data isn't available (for example, the schema expired), the
  query endpoint returns 409 with the recovery state, and a read-write member
  can start a repair.
- Artifacts show their creator's name, or "Deleted user" if the account was
  deleted.

---

## Recipes

A recipe is a saved prompt template with typed variables, created by the agent's
`save_as_recipe` tool.

- All members can view recipes and their runs. There is no per-recipe or
  per-run sharing setting.
- Read-write members can edit and soft-delete recipes.
- Any member can run a recipe. The run is queued and executed by the
  background worker; the API returns 202 with the pending run.
- A run executes as the member who started it, headless, with that member's
  role. If data needs loading, the run loads it and continues. A run that ends
  on an escalation or error is marked failed.

---

## Workspace knowledge

- **KnowledgeEntry**: free-form markdown entries with a title and tags.
  Read-write members create, edit and delete them, and can import or export
  them as a zip.
- **TableKnowledge**: table descriptions, column notes, data-quality notes and
  related tables. Read-write members edit them through the data dictionary.
- **AgentLearning**: corrections the agent saves with `save_learning`. They
  can't be created by hand; read-write members can edit or delete them.

All members can view workspace knowledge. All three are included in the
agent's prompt (see [Agent architecture](agent.md#4-knowledge-context)).

---

## Multi-tenant workspaces

A workspace with two or more tenants is served from its own view schema
(`ws_<id>`). It holds one view per table of each tenant, namespaced by a tenant
prefix; tables from different tenants are not joined or unioned. Tenants
without loaded data are left out and reported as excluded. A workspace with a
single tenant queries that tenant's schema directly.

- The view schema has its own read-only role and expires on its own 24-hour
  timer. When it has expired but the tenant schemas are still active,
  recovery only rebuilds the views. Expired tenant schemas are loaded again
  first.
- Only managers can add or remove tenants. Adding a tenant is refused (409) if
  any member can't use it. The last tenant can't be removed.
- Adding or removing a tenant rebuilds the view schema in one transaction.
  During the rebuild the schema is marked provisioning, and queries fail until
  it finishes. If a rebuild fails, the transaction rolls back and the previous
  views serve again, with the error recorded; a failed first build is marked
  failed. Adding a tenant with no data yet keeps the workspace queryable and
  starts a load for it.
- Removing tenants down to one drops the view schema.
