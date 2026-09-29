# Sharing

Scout shares work through workspace membership. To give someone access to your analysis, add them to the workspace (see [Workspaces](../admin/workspaces.md)).

## What workspace members can see

| Item | Visible to |
|------|------------|
| **Artifacts** | Every member of the workspace, on the **Artifacts** page |
| **Recipes and recipe runs** | Every member of the workspace, on the **Recipes** page |
| **Knowledge entries and learnings** | Every member of the workspace, on the **Knowledge** page |
| **Chat threads** | Only the member who started the thread |

What a member can change depends on their workspace role. **Read** members can view and run things; **Read-Write** and **Manager** members can also create and edit them.

## Public links

There is no share-link UI in Scout, and artifacts cannot be shared outside the workspace. Two items support public, read-only links through the API only:

- **Chat threads.** `PATCH /api/workspaces/<workspace_id>/threads/<thread_id>/share/` with `{"is_shared": true}` returns a `share_token`. Anyone can then read the thread's messages at `/shared/threads/<share_token>`, without logging in. Only the thread's owner can share it, and turning sharing on requires the **Read-Write** or **Manager** role. Setting `is_shared` back to `false` clears the token, so the link stops working.
- **Recipe runs.** `PATCH /api/workspaces/<workspace_id>/recipes/<recipe_id>/runs/<run_id>/` with `{"is_public": true}` (requires **Read-Write** or **Manager**) generates a `share_token`. The run's results are then readable at `/shared/runs/<share_token>` without logging in.

Public links have no expiry and no access levels: anyone with the link can read the content until sharing is turned off.
