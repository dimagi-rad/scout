# Remove Projects, Scope to TenantWorkspace

## Context

Scout originally used a Project model where users created projects with SQL connection params and the agent queried the project DB. The codebase has been migrated to an OAuth-based model where users authenticate with CommCare, the agent materializes data from the CommCare API into a managed DB schema, and then queries that schema.

In the new model, Projects serve no purpose. The chat view already only uses the tenant flow. However, several workspace features (artifacts, learnings, recipes, knowledge) still require a Project FK, so they are unavailable in the tenant flow.

## Decision

- Introduce `TenantWorkspace` as a per-tenant entity holding agent config and serving as FK target for workspace models.
- Delete Project, ProjectMembership, DatabaseConnection, ProjectRole, GoldenQuery, EvalRun.
- Re-scope Artifact, Recipe, AgentLearning, KnowledgeEntry, TableKnowledge to TenantWorkspace.
- Drop allowed_tables/excluded_tables concept entirely.
- No data migration or backwards compatibility required.

## New Model: TenantWorkspace

Lives in `apps/projects/models.py` alongside TenantSchema and MaterializationRun.

```python
class TenantWorkspace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    tenant_id = models.CharField(max_length=255, unique=True)
    tenant_name = models.CharField(max_length=255)
    system_prompt = models.TextField(blank=True)
    data_dictionary = models.JSONField(null=True, blank=True)
    data_dictionary_generated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
```

- `tenant_id` is unique (one workspace per tenant across all users).
- Auto-created on first chat for a tenant.
- `system_prompt` enables per-tenant agent customization.
- `data_dictionary` caches schema info from materialized tables.

## Models to Delete

| Model | App | Reason |
|---|---|---|
| Project | projects | Replaced by TenantWorkspace |
| ProjectMembership | projects | Replaced by TenantMembership |
| ProjectRole | projects | No longer needed |
| DatabaseConnection | projects | Replaced by MANAGED_DATABASE_URL |
| GoldenQuery | knowledge | Dropped (not useful with dynamic schemas) |
| EvalRun | knowledge | Dropped (paired with GoldenQuery) |

## Models to Re-scope

| Model | Old FK | New FK |
|---|---|---|
| Thread | project (nullable) | Remove project FK, keep tenant_membership |
| Artifact | project | workspace -> TenantWorkspace |
| KnowledgeEntry | project | workspace -> TenantWorkspace |
| TableKnowledge | project | workspace -> TenantWorkspace |
| AgentLearning | project | workspace -> TenantWorkspace |
| Recipe | project | workspace -> TenantWorkspace |

## SharedArtifact Changes

- Rename AccessLevel.PROJECT -> AccessLevel.TENANT.
- `can_access()` checks TenantMembership for the artifact's workspace tenant_id.

## MCP Context Changes

Rename ProjectContext -> QueryContext:

```python
@dataclass(frozen=True)
class QueryContext:
    tenant_id: str
    schema_name: str
    max_rows_per_query: int = 500
    max_query_timeout_seconds: int = 30
    connection_params: dict[str, Any]
```

- Drop: project_id, project_name, allowed_tables, excluded_tables, readonly_role.
- Delete load_project_context().
- load_tenant_context() returns QueryContext.

## Agent Graph Changes

- Remove `project` parameter from build_agent_graph().
- Always use tenant_membership flow.
- Always create local tools (artifact, learning, recipe) using workspace.
- System prompt: BASE_SYSTEM_PROMPT + workspace.system_prompt + knowledge retriever + data dictionary.
- Single injection map: tenant_id + tenant_membership_id.

## Tool Factory Changes

Each tool factory takes `workspace: TenantWorkspace` instead of `project: Project`:
- create_save_learning_tool(workspace, user)
- create_artifact_tools(workspace, user)
- create_recipe_tool(workspace, user)

## Services to Delete/Simplify

| Service | Action |
|---|---|
| projects/services/db_manager.py | Delete |
| projects/services/data_dictionary.py | Refactor to work with QueryContext |
| projects/api/ (all) | Delete project CRUD, permissions, serializers, connections, csv_import |
| knowledge/services/retriever.py | Refactor: KnowledgeRetriever(workspace) |
| knowledge/api/views.py | Re-scope to workspace |
| chat/views.py | Remove project references |

## Chat View Changes

- Remove _get_membership() helper.
- Remove project_id from _upsert_thread(), _list_threads(), thread_list_view().
- thread_list_view only accepts tenant_id.
- Auto-create TenantWorkspace on first chat if it doesn't exist.

## Frontend Impact

Minimal. The frontend already uses the tenant/domain selector. Remove any remaining project references in API calls.
