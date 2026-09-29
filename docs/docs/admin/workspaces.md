# Workspaces

A workspace is where a team works in Scout. It is layered over one or more **data sources** (tenants): a CommCare HQ project space, a CommCare Connect opportunity, or an Open Chat Studio chatbot. Chat threads, artifacts, recipes, and knowledge all belong to a workspace.

## Creating a workspace

Scout creates workspaces automatically. When you log in with CommCare HQ, CommCare Connect, or Open Chat Studio, Scout finds the data sources your account can access and creates a workspace for each new one, with you as its manager.

To combine sources or pick a custom name, choose **New workspace** in the workspace switcher. Enter a name and select one or more data sources. You can only select sources your own account can use. The creator becomes the workspace's manager.

## Managing a workspace

Open a workspace's settings with the gear icon next to its name in the workspace switcher, or choose **Manage workspaces** in the switcher to see all your workspaces. The page has three tabs.

### Members

Lists the members and their roles, plus any pending invites. Managers can:

- **Add a member** by email and role.
- **Change a member's role**.
- **Remove a member**. This also deletes all of that member's chat threads in the workspace, and cannot be undone.

A workspace always keeps at least one manager, so the last manager cannot be removed or demoted. Other members can only leave through the API (`DELETE /api/workspaces/<workspace_id>/members/<membership_id>/` on their own membership), which deletes their threads in the same way.

When a manager adds someone by email:

- **The person has no Scout account.** Scout creates a *pending* invite and emails them. The invite resolves when they first log in with that (verified) email.
- **The person has an account but can't use every data source in the workspace.** Scout creates an *awaiting access* invite and emails them that they first need access to the workspace's data sources. It resolves once they have access to every source and log in again.
- **Otherwise** they become a member straight away.

Invites expire after 30 days.

### Data sources

Lists the workspace's data sources. Managers can add a source they can access themselves, or remove one. Every member must be able to use every source, so Scout refuses to add a source that some existing member cannot access.

### Settings

- **Workspace name**: managers can rename the workspace.
- **System prompt**: custom instructions added to the agent's system prompt for every conversation in this workspace. Only managers can edit it.
- **Danger zone**: managers can delete the workspace and all its threads. This cannot be undone.

## Roles

Each member has one role per workspace. A user can have different roles in different workspaces.

| Role | Label in the UI | What it allows |
|------|-----------------|----------------|
| `read` | Read | Chat with the agent, view artifacts, recipes, and knowledge, run recipes, and export knowledge. The agent only gets read-only tools: it cannot save artifacts, recipes, or learnings, change the data model, or refresh data. |
| `read_write` | Read-Write | Everything in **Read**, plus: create and edit artifacts, recipes, and knowledge; save datasets to the data model; refresh data (`/refresh-data`); share a chat thread. |
| `manage` | Manager | Everything in **Read-Write**, plus: add and remove members and invites, change roles, add and remove data sources, edit the system prompt, rename and delete the workspace. |
