# Scout Core Design Specification

## Overview

Scout is a multi-tenant data exploration platform that allows users to query, analyse, and collaborate on data pulled from external systems. Users interact with the system through a chat interface backed by an AI agent, and can produce persistent artifacts and recipes from their analyses. Users interact with tenants primarily through workspaces rather than directly.

---

## Tenants

### Definition

A tenant represents a scoped access point to data in an external system. For example, a "domain" in CommCare. Tenants are the foundation of data isolation in Scout.

### Creation

Tenants are auto-created in Scout when:
- A user logs in for the first time via OAuth from an external system, or
- A user adds a connection to a new external system via OAuth.

New tenants that have become available since the user last logged in can be picked up by selecting "Refresh access" on the connection management page. This re-validates the user's access against the external system and creates any new tenant memberships and workspaces.

### Data Storage

- Each tenant is backed by a dedicated, isolated PostgreSQL schema.
- There is a one-to-one relationship between a tenant and a PostgreSQL schema.
- Data stored in tenant schemas is read-only and sourced from the external system.
- If multiple users have access to the same tenant, they share the same PostgreSQL schema to avoid data duplication.
- Tenant data includes system-generated metadata that augments the raw data (e.g. case property names). This metadata is stored outside the schema.

### Schema Lifecycle

- A tenant schema is dropped if there has been no user activity for 24 hours. Only user-initiated actions count toward the inactivity timer — system or background activity does not reset it. This prevents automated processes from keeping schemas alive indefinitely.
- Any user activity on a workspace linked to a tenant counts as activity for that tenant. For multi-tenant workspaces, user activity counts for all connected tenant schemas simultaneously.
- Each schema's TTL is evaluated independently. Expiring a multi-tenant workspace's view schema does not cascade to the underlying tenant schemas, as those may be in use by other workspaces. Equally, a tenant schema being kept alive by one workspace does not keep any other workspace's view schema alive.
- Metadata about the tenant is stored outside the schema and is retained even after the schema is dropped.
- When a user returns to a tenant after the schema has been dropped, the data is re-fetched from scratch from the external system. This is treated the same as a first-time access. The re-fetch begins lazily — only when the user first takes an action that requires data access (e.g. sending a message in chat, refreshing an artifact, or running a recipe). Selecting a workspace in the UI does not by itself trigger a re-fetch.
- Workspaces whose schema has been dropped appear in the workspace list in a clearly marked "data unavailable" state. The workspace remains browsable (thread list, artifact list, recipes, etc.) but data access is blocked. The re-fetch is triggered when the user performs an action that requires data, at which point a progress UI is displayed.

### Data Refresh

- Tenant data is refreshed on demand. Scheduled refresh may be added in future.
- Any user with write or manage role on a workspace can trigger a refresh.
- A refresh creates a new schema in the background. The old schema remains in use until the new one is ready. On success the workspace cuts over to the new schema immediately and the old one is dropped. On failure the workspace stays on the old schema and an error is shown.
- In multi-tenant workspaces, refresh can be triggered per tenant or for all tenants simultaneously. The same new-schema/cutover approach applies.
- Parallel refreshes are rate-limited to avoid overloading external systems. When a user triggers a refresh that cannot begin immediately due to rate limiting, it is queued. The UI distinguishes between "refresh in progress" and "refresh queued" states. The specific rate limits are configurable per deployment and are not defined in this specification.

---

## Workspaces

### Definition

A workspace is a layer on top of one or more tenants. It stores customisable data and metadata, and is the primary interface through which users interact with Scout.

### Auto-Created Workspaces

- The system automatically creates a single-tenant workspace for each tenant a user has access to.
- Auto-created workspaces are created at the same time as tenant membership, so they are available when the user first accesses the UI.
- Auto-created workspaces can be renamed by the user.

### Workspace Contents

A workspace contains:
- Threads
- Artifacts
- Recipes
- TableKnowledge, KnowledgeEntry, AgentLearning (workspace-level knowledge)

### Workspace Management

- Users can create new workspaces and delete existing ones.
- A user cannot delete a workspace if doing so would leave them with no workspace for any of the underlying tenants. The user must create a replacement workspace covering that tenant before deletion is permitted.
- Deleting a workspace removes all associated data including threads, artifacts, recipes, and knowledge.
- Any shared thread links are immediately invalidated when a workspace is deleted.
- Any pending invitations to a deleted workspace are cancelled and invitees are notified.
- If no manage-role user remains in a workspace, the workspace is not automatically removed. Instead, see the Losing Access section for how this is handled.

### Tenant Access Validation

- Tenant access is validated against the external system in the following situations: when the local validation token has expired (after 24 hours), when the user creates a new workspace, when the user accepts a workspace invitation, or when the user switches workspace in the UI.
- All other requests check only the local Scout validation token, which is a lightweight in-process check.
- If external re-validation definitively confirms that the user has lost access, the user is immediately removed from the affected workspace and the standard removal and cleanup process is applied (see User Roles & Permissions — Losing Access).
- If external re-validation fails due to an error (e.g. the external system returns a 5xx response), the user is temporarily suspended on the affected tenant. Suspension is scoped per tenant — only workspaces backed by the affected tenant are suspended; other workspaces are unaffected. The user sees an error message and must retry manually. There is no automatic retry. Validation errors do not constitute revocation of access.
- If the suspended user does not retry and the tenant schema's TTL expires in the meantime, the schema is dropped as normal. Suspension does not count as activity and does not reset the TTL.
- Once a suspended user retries and validation succeeds, they must re-authenticate before resuming their session.
- OAuth token expiration does not count as losing tenant access. The user is allowed to re-authenticate inline, ideally via a popup or modal without leaving the page. Full redirect with return-to-URL is the baseline if inline re-auth is not feasible.
- After successful re-authentication, the user is returned to where they were.

---

## User Roles & Permissions

### Roles

Each workspace member is assigned one of the following roles:

- **Read** — Can view threads (read-only), artifacts, recipes, recipe run outputs, and knowledge. Cannot trigger data refreshes, run recipes, or edit content.
- **Read/Write** — Can create and edit threads, artifacts, and recipes. Can trigger data refreshes and run recipes. Can soft-delete and undelete artifacts and recipes. Can create, edit, and remove TableKnowledge and KnowledgeEntry entries. Can edit and remove AgentLearning entries.
- **Manage** — All write permissions plus: invite and remove users, assign any role (including Manage), view the audit log, and manage workspace settings including adding/removing tenants.

### Access Control Notes

- There is no separate "owner" role. Any manage-role user can perform all management actions including deleting the workspace.
- Manage-role users can assign any role, including Manage, to other users.
- A manage-role user cannot be removed or demoted if doing so would leave no manage-role user in the workspace. The action is blocked until another member is assigned the manage role first.
- Superusers have full write access across all workspaces, in addition to read access to all workspace metadata and audit logs. Superusers can read and manage recipes, TableKnowledge, KnowledgeEntry, and AgentLearning entries. Superusers cannot view thread contents, artifact contents, recipe run outputs, or any other content that may contain actual tenant data.

### Losing Access

- If a user loses access to an underlying tenant (as detected by the external system), they are removed from any workspace backed by that tenant. Detection is checkpoint-based; there is an inherent window of up to 24 hours between validation checks during which a user who has lost external access may continue to use the system.
- Removal is handled uniformly regardless of how loss of access was detected (explicit re-validation or background checkpoint). Access is immediately revoked and cleanup actions are taken immediately or scheduled for the immediate future.
- When a manage-role user is automatically removed due to lost tenant access and this would leave no manage-role user in the workspace, the longest-standing write-role member is automatically promoted to manage role. If no write-role member exists, the workspace is flagged for superuser review. Superusers can resolve this by assigning a new manager or deleting the workspace at their discretion.
- When a user is removed from a workspace, their threads are deleted. Their artifacts and recipes remain and are visible to other workspace members, attributed to their real name.

---

## Invitations

### Inviting Users to a Workspace

- Invitations are sent via the UI by workspace manage-role users. The inviter specifies the invitee's role at the time of sending.
- The invitee receives an email notification. If they already have a Scout account they can also view and accept the invitation directly in the UI.
- If the invitee does not have a Scout account, they must register before accepting.
- Before accepting an invitation, the invitee must have logged in via OAuth and connected an OAuth account for the external system(s) used by the workspace.
- Tenant access validation for the invitee can only occur at acceptance time, as a valid auth token is required.
- If the invitee does not have access to the required tenants, they see an error message (without revealing which specific tenants they are missing access to).
- The user who sent the invitation is notified of invitation failures and can view the invitation status including any error message.
- If the inviter's role is downgraded below manage, or the inviter is removed from the workspace, all of their pending invitations are silently revoked with no notification to the invitees.
- Invitations expire after 7 days. Any manage-role user can re-invite a user after expiry — it is not restricted to the original inviter. Re-inviting issues a fresh invitation subject to the normal role assignment and permission rules.
- For multi-tenant workspaces, the invitee must have access to all connected tenants.

---

## Threads

A thread is a chat session between a user and the AI agent. Threads are associated with a workspace and owned by the user who started them.

- Threads are private by default.
- Threads are deleted if the owning user loses access to the workspace.

### Sharing Threads

- Threads can be shared with other workspace members via a shareable link.
- Manage-role users can additionally extend access to specific named Scout users who are not workspace members. This grants those users read access to that thread only — they do not gain any other workspace access and do not appear as workspace members.
- If a named non-member granted thread access is also a workspace member, their workspace role takes precedence over the thread-level grant.
- Shared threads are read-only.
- Thread sharing can be revoked. All recipients, including any named non-members, lose access immediately upon revocation.

---

## Artifacts

Artifacts are persistent, reusable outputs produced during a thread, either by the user or the agent. They can render text, charts, tables, and other content. Artifacts are tied to specific data from the underlying tenant schema and can be refreshed.

### Visibility & Access

- Artifacts are visible to all workspace members, including read-only users.
- Artifacts can be created and edited by users with write or manage role.
- Artifacts can be deleted (soft delete) by users with write or manage role.
- Soft-deleted artifacts are only visible to write and manage users.
- Write and manage role users can undelete soft-deleted artifacts.
- Hard delete is manual for now; there is no automatic purge.
- If a deleted artifact is referenced in a thread, the link remains visible but shows a "deleted" message with an option to undelete (visible to write and manage users only).
- Artifacts cannot be shared publicly due to the risk of containing sensitive data.
- Artifacts are attributed to their creator. If a user is removed from the workspace but their account still exists, their real name is shown. If their account has been deleted, they are attributed as "Deleted user".

### Artifact Refresh

- Artifact refresh runs against the last-synced snapshot of the tenant data. It does not trigger a refresh of tenant data from the external system.
- Artifact refresh can only be triggered manually by users with write or manage role.
- If the underlying tenant schema has been dropped (due to inactivity), artifact refresh fails gracefully and the user is prompted to re-sync tenant data first.

---

## Recipes

Recipes are a set of steps that describe how to perform an analysis. Unlike artifacts, they are procedural rather than output-focused.

### Visibility & Access

- Recipes are visible to all workspace members, including read-only users.
- Recipes can be created and edited by users with write or manage role.
- Recipes can be deleted (soft delete) by users with write or manage role.
- Write and manage role users can undelete soft-deleted recipes.
- Read-only users cannot run recipes.
- Recipes are attributed to their creator using the same rules as artifacts (real name if account exists, "Deleted user" if account deleted).

### Running Recipes

- Recipes can be run by users with write or manage role.
- When run, a recipe produces an analysis output visible to all workspace members including read-only users.
- If the underlying tenant schema has been dropped, recipe runs fail gracefully and the user is prompted to re-sync tenant data first.
- If a recipe is mid-run when the tenant schema is dropped or the workspace is deleted, the run is marked as failed, an error is recorded in the run history, and the user who triggered the run is notified.

> **Note:** The recipe output model is a placeholder for the initial build and should not be over-engineered. The long-term solution for run history and output management is unresolved and will be addressed in a future iteration.

---

## Workspace Knowledge

Workspaces contain shared knowledge that is available to all workspace members. This includes:

- **TableKnowledge** — structured knowledge about data tables. Manually created and edited by write and manage role users.
- **KnowledgeEntry** — general knowledge entries. Manually created and edited by write and manage role users.
- **AgentLearning** — learnings derived automatically from agent interactions. Cannot be manually created, but write and manage role users can edit or remove individual entries.

All workspace members, including read-only users, can view workspace knowledge.

---

## Multi-Tenant Workspaces

### Definition

In addition to auto-created single-tenant workspaces, users can create workspaces linked to multiple tenants. These are multi-tenant workspaces.

### Database Structure

- A multi-tenant workspace is backed by a single PostgreSQL schema containing views that join data from the individual tenant schemas.
- Each tenant's underlying data schema remains independent.
- The workspace view schema and each underlying tenant schema each have their own independent TTL, evaluated in isolation. Expiring the workspace view schema does not cascade to the underlying tenant schemas.

### Schema Recovery

If the workspace view schema expires but the underlying tenant schemas are still alive (kept active by other workspaces), recovery requires only a view rebuild — no re-fetch from the external system is needed. The rebuild follows the same flow as an initial build: users can browse the workspace but data access is blocked until the build completes.

If both the workspace view schema and one or more underlying tenant schemas have expired, those tenant schemas must be re-fetched from the external system before the views can be rebuilt.

Note: it is not possible for an underlying tenant schema to expire while a multi-tenant workspace using it remains active, since user activity on the workspace simultaneously extends the TTL of all its underlying tenant schemas. The only way a multi-tenant workspace loses its underlying data is if the workspace itself goes inactive for 24 hours.

### Adding & Removing Tenants

- Tenants can be added or removed from a multi-tenant workspace by manage-role users.
- Adding or removing a tenant triggers a rebuild of the workspace's database views.
- During a view rebuild, the old schema remains usable. A notice is displayed to users while the rebuild is in progress.
- Build progress is shown to active users and on the workspace details page.
- After a successful rebuild, the workspace cuts over to the new schema immediately. The old schema is dropped immediately after cutover.
- If the build fails, the workspace stays on the old schema and an error notice is displayed.

### Inviting Users to Multi-Tenant Workspaces

- When inviting a user to a multi-tenant workspace, the system validates that the invitee has access to all connected tenants at acceptance time.
- If the user lacks access to any tenant, the invitation fails with an error visible to the user who sent the invitation.

---

## Account Deletion

When a user deletes their account, all data private to that user is deleted. Data shared with other workspace members is retained.

- **Threads** — deleted.
- **Artifacts and recipes** — retained and remain visible to other workspace members, attributed as "Deleted user".
- **Sole-member workspaces** — deleted via the normal workspace deletion path (shared thread links invalidated, pending invitations cancelled, audit log entry created).
- **Shared workspaces** — persist. If the user is the sole manager of any shared workspace, account deletion is blocked until they assign a replacement manager. The deletion flow surfaces all such workspaces in a single step, requiring the user to assign a replacement manager for each before proceeding.
- **Pending invitations** — both sent and received invitations are revoked on account deletion.
- **Audit logs** — retained after account deletion, including the user's real identity. This is a deliberate exception to the "Deleted user" display policy; audit logs preserve real identity to maintain a meaningful record of data access for compliance purposes.

---

## Audit Log

Scout maintains an audit log of key actions within workspaces.

- Logged actions include: invitations, role changes, user removals, workspace deletions, data refreshes, artifact and recipe deletions, and tenant add/remove operations.
- The audit log is searchable, filterable, and exportable.
- The audit log is read-only. No actions can be taken through the audit log interface.
- Workspace manage-role users can view the audit log for their own workspace.
- Superusers can view the audit log across all workspaces.
- Audit logs are retained after account deletion, including the real identity of the deleted user.

---

## Limits

- There are no limits on the number of workspaces a user can create.
- There are no limits on the number of tenants that can be attached to a multi-tenant workspace.

---

## Known Design Debt & Out of Scope

### Known Design Debt

- **Recipe output model:** The current approach is a minimal placeholder. The long-term solution for run history and output management is unresolved and should not be over-engineered in the initial build.
- **Tenant data refresh schedule:** On-demand only for now. Scheduled refresh is a future consideration.

### Out of Scope (Future)

- **Group threads:** Multiple users chatting with the agent simultaneously.
- **Knowledge/learning contribution back to the tenant:** A "publish to catalog" model for sharing knowledge across workspaces attached to the same tenant.
- **Scheduled tenant data refresh.**
