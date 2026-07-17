# Cross-report contradictions — arch review (undated run)

Reviewer: contradiction-resolution pass over all 36 reports in
`docs/arch-review/2026-06-12/reports/`. Mandate: find claims where two or more reports
disagree about the *same component* (complete vs partial, different counts, one praises
what another condemns), resolve each against the actual code at HEAD `35e4230`, and
record the evidence.

Method: every resolution below was checked against the current source (grep / file read),
not adjudicated by reviewer count. Where the majority is right, that is stated; where the
majority is wrong, that is stated too. "Not a contradiction" entries are included at the
end because they look like disagreements but are reconcilable (different surfaces, different
scopes) — listing them prevents re-litigating them.

The reports are remarkably consistent on the headline findings (refresh data-loss, recipe
runner signature drift, MCP `teardown_schema` state drift, multi-tenant artifact routing,
the 63-byte identifier class, the dead oauth-token transport). The genuine contradictions
are narrower and are listed first.

---

## C1 — Is `apps/workspaces/permissions.py` dead code, or imported by 4 view modules?

**The disagreement.**
- **Dead / zero importers:** `lens-dead-code` (F2, "zero importers anywhere", with the grep),
  `generalist-1` (F6, "zero imports anywhere"), `generalist-2` (F4), `generalist-3` (F6),
  `lens-security-authz` (S-7 / §2, "imported nowhere — confirmed"), `lens-consistency`
  (Family A5, "zero callers — dead code", grep shown), `vertical-tenancy-sharing`
  (T4, "**zero imports** anywhere ... confirmed by grep"), `journey-new-user` (F-roles).
- **NOT dead:** `lens-test-architecture` — in its skimmed coverage note on
  `tests/test_workspace_permissions.py`: *"note: permission classes ARE imported by 4 view
  modules, so the v1 'dead code' claim deserves separate adjudication, not done here."*
- **Explicitly unverified:** `seam-accounts-tenancy-sharing` honestly hedges — *"whether
  permission classes from permissions.py [are imported] was NOT verified per-route (URLConf
  not read)."*

**Resolution: the classes are dead code. The test-architecture lens is wrong.**

**Evidence.** A repo-wide grep for the three class names finds only their definitions:
```
$ grep -rn "IsWorkspaceMember\|IsWorkspaceReadWrite\|IsWorkspaceManager" apps/ config/ mcp_server/ tests/
apps/workspaces/permissions.py:21:class IsWorkspaceMember(BasePermission):
apps/workspaces/permissions.py:28:class IsWorkspaceReadWrite(BasePermission):
apps/workspaces/permissions.py:36:class IsWorkspaceManager(BasePermission):
```
No `from apps.workspaces.permissions import ...` exists anywhere outside the module itself.
Every DRF view uses `permission_classes = [IsAuthenticated]` and re-implements role checks
inline (the eight enforcement records the other reports map: `workspace_views.py:302` etc.).
The test-architecture lens appears to have conflated "a test file named
`test_workspace_permissions.py` exists" with "the permission classes are imported by views,"
and labeled it for later adjudication rather than running the grep. The eight other reports'
grep-backed claim is correct.

---

## C2 — Can the agent/LLM invoke `cancel_materialization` and `get_materialization_status`?

**The disagreement.** Both tools are absent from `MCP_TOOL_NAMES`. Reports split on what
that means for reachability:
- **Agent CAN see/call them (they pass through unmodified):** `generalist-1` (F12),
  `generalist-3` (F5, "_build_tools passes all MCP tools through ... the agent supplies
  run_id itself"), `vertical-mcp-server` (F5, "_llm_tool_schemas passes tools outside
  MCP_TOOL_NAMES through unchanged"), `vertical-tenancy-sharing` (T6, "their schemas pass to
  the LLM unmodified ... all MCP tools are bound"), `journey-failure-paths` (F3, "all 11 MCP
  tools are bound ... callable by the model").
- **Agent never sees them:** `lens-consistency` (Family B, "**not** in MCP_TOOL_NAMES ... so
  the agent never sees them; only a direct MCP client can hit B3 today"), `lens-security-authz`
  (S-6, "the LLM cannot supply run_id for another workspace under normal flow" — framed as
  if unreachable), `lens-data-integrity` (F8, "**not** in MCP_TOOL_NAMES, so the agent...").

**Resolution: the agent CAN see and call both tools. The "never sees them" camp is wrong on
mechanism; the "limited reachability" nuance both camps gesture at is real but separate.**

**Evidence.** `apps/agents/graph/base.py`:
- `_build_tools` (line 692): `tools = list(mcp_tools)` — *every* MCP tool is bound to the agent.
- `_llm_tool_schemas` (lines 407-410): `for tool in tools: if tool.name not in MCP_TOOL_NAMES:
  result.append(tool); continue` — tools NOT in `MCP_TOOL_NAMES` are appended **unchanged**
  (full raw schema, visible to the LLM). The set only controls (a) hiding context params and
  (b) server-side arg injection — not whether the tool is offered to the model.

So `cancel_materialization`/`get_materialization_status` ARE offered to the LLM with their raw
`run_id` parameter. `lens-consistency`'s claim "the agent never sees them" is mechanically
false. The mitigating nuance (which `generalist-1/3`, `vertical-mcp`, `journey-failure` all
also state) is that under the fire-and-ack flow the agent never legitimately *learns* a
`run_id` — `run_materialization` returns `thread_job_id`, not `run_id` — so the model would
have to hallucinate or scrape a UUID. That bounds exploitability, but it is a different claim
from "the agent never sees the tool." The contradiction is on visibility, and the majority is
correct.

---

## C3 — Do public/shared threads ever display artifacts?

**The disagreement.**
- **Always zero artifacts — `conversation_id` is never populated:** `generalist-1` (F7),
  `generalist-2` (F9), `generalist-3` (F8), `vertical-recipes-knowledge` (adjacent),
  `seam-schema-references` — all say `create_artifact_tools` is called without
  `conversation_id`, so every artifact stores `conversation_id=""`, and the public-thread
  consumer filters `conversation_id=str(thread_id)` → always empty.
- **Public thread page serializes & renders artifact code/data:** `vertical-artifacts`
  (ART-6, "The public thread page renders artifacts by dumping their source code as
  preformatted text" — describing what the page does with the artifacts it receives) and
  `vertical-tenancy-sharing` (T7, public thread endpoint "serve[s] messages **plus every
  artifact's full code and data**").

**Resolution: not a true logical contradiction, but a trap worth recording — the artifact
array on shared threads is *always empty in practice*, so ART-6/T7's "shows artifact code"
describes rendering logic that never receives any input. The `conversation_id` camp is
correct about the operative outcome.**

**Evidence.**
- Producer: `apps/agents/graph/base.py:694` calls `create_artifact_tools(workspace, user)` —
  no `conversation_id` arg. The factory's third param defaults to `None`
  (`artifact_tool.py:56-57`) and the create path stores `conversation_id=conversation_id or ""`
  (`artifact_tool.py:197`). So every chat-created artifact has `conversation_id == ""`.
- Consumer: `apps/chat/thread_views.py:65` —
  `Artifact.objects.filter(conversation_id=str(thread_id))`. `str(thread_id)` is a UUID string,
  never `""`, so this matches zero rows; `public_thread_view` (`thread_views.py:245`) attaches
  that empty list.

`vertical-artifacts`'s own coverage log admits it did **not** trace whether `conversation_id`
is set ("Whether `conversation_id` ... is reliably set ... was NOT examined"), and reasoned
about the serialization shape from the response schema. So ART-6/T7 correctly describe what
the page/endpoint *would* do with artifacts, but the array they operate on is always empty.
The two generalist-camp and artifacts-camp claims are reconcilable; the load-bearing fact is
that shared threads show zero artifacts today.

---

## C4 — Is the embed widget (`/widget.js`) live and working in production?

**The disagreement.**
- **Live / wired / working:** `vertical-artifacts` (ART-8, "Embed/widget SDK ~70% ... loads
  iframe, tenant select works"), `vertical-tenancy-sharing` (T7, "/widget.js + /embed are
  intentional and wired ... Not drift"), `seam-backend-frontend` ("widget.js ships in the API
  image ... so the `/scout` prefix works for the widget↔embed handshake").
- **Unreachable on the primary production host:** `lens-ops-config` (F10, "`frontend/nginx.prod.conf`
  (labs) proxies `/scout/widget.js` to Django, but `frontend/nginx.prod-kamal.conf`
  (scout.dimagi.com) has **no `/widget.js` location** — it falls through to the SPA catch-all
  and serves `index.html`").

**Resolution: both are right about different environments. The widget code is wired and works
on labs (`/scout/` mount), but the primary-prod nginx config (`nginx.prod-kamal.conf`) does
not route `widget.js`, so the SDK is effectively dead on scout.dimagi.com.**

**Evidence.**
```
$ grep -n widget frontend/nginx.prod.conf
59:    location = /scout/widget.js {
60:        alias /usr/share/nginx/html/widget.js;
$ grep -n widget frontend/nginx.prod-kamal.conf
(no output)
```
The backend route (`config/views.py` → `widget_js_view`) and the image-baked
`frontend/public/widget.js` both exist (so the artifacts/tenancy/seam reports are right that
the *code* is wired), but the primary-prod reverse proxy has no `widget.js` location block, so
a request to `https://scout.dimagi.com/widget.js` falls through to the SPA catch-all and
returns `index.html`. The ops lens caught a real per-environment gap the feature-vertical
reports missed because they read the labs nginx (or the code) and not the kamal-prod nginx.

---

## C5 — Does the legacy `/refresh/` path's TTL-touch fix count as "applied to siblings"?

**The disagreement (subtle).**
- `lens-consistency` (Family G / "What's fine": "Post-#228 TTL resets on activation exist at
  **both** provisioning sites (`schema_manager.py:115-123`, `tasks.py:179-184`) — the fix was
  applied to siblings") and `vertical-tenancy-sharing` ("What's fine": provision resurrect and
  the refresh activation both reset `last_accessed_at`) present the refresh path's TTL handling
  as a *correctly-applied* sibling fix.
- `vertical-materialization` (F1), `generalist-1/2/3` (F1), `journey-failure-paths` (F1),
  `lens-test-architecture` (T3), `seam-schema-references`, `vertical-catalog-dictionary` (F1)
  all condemn the same refresh path as **actively destructive** (loads into the old schema,
  activates an empty `_r` schema, tears down the data-bearing one).

**Resolution: not a contradiction — they are praising and condemning different lines of the
same function.** The refresh task *does* correctly reset `last_accessed_at` when it activates
the new `_r` schema (`tasks.py:182-184`), so the narrow #228 TTL-touch invariant is upheld at
both sites. But that is orthogonal to the data-loss bug: the `_r` schema it touches is *empty*
because `run_pipeline` loaded into the base schema (`materializer.py:183` via `provision()`),
and the task then tears down the data-bearing base schema. The consistency/tenancy reports
are correct that the TTL-touch sibling was applied; the materialization-camp reports are
correct that the path is still data-destroying. Recorded here because reading C5 in isolation
("the fix was applied to siblings" next to "this path destroys data") looks contradictory.

**Evidence.** `apps/workspaces/tasks.py:182-184` sets `state=ACTIVE` and
`last_accessed_at=timezone.now()` on `new_schema` (the TTL fix). `mcp_server/services/
materializer.py:183` (`run_pipeline`) calls `SchemaManager().provision(tenant)`, which resolves
the base name (`schema_manager.py:66-78`) and loads there — the empty-`_r` data-loss mechanism
every other report traces.

---

## C6 — Is the role model "unenforced" or is workspace-*management* role enforcement "correct and consistent"?

**The disagreement.**
- Broad framing as "roles ~unenforced" / "barely enforced": `generalist-1` (F6),
  `generalist-2` (F4), `generalist-3` (F6), `vertical-tenancy-sharing` (T4, "~75% unenforced").
- Explicit pushback: `lens-security-authz` — *"Role enforcement on workspace management is
  correct and consistent (verified-by-trace). This contradicts the seed 'roles ~unenforced':
  the management surface enforces MANAGE."* `vertical-frontend` ("What's fine") and
  `vertical-tenancy-sharing` ("What's fine") agree the management routes enforce role correctly.

**Resolution: not a true contradiction — both are right because they scope to different
endpoint families.** The security/frontend reports are correct that workspace *management*
endpoints (rename, delete, member add/role-change/remove, tenant add/remove) consistently
enforce MANAGE; the generalist/tenancy "unenforced" claims are correct that the *content*
surface (knowledge, recipes, artifacts, chat-driven materialization/teardown) checks only
membership. The apparent disagreement is an altitude difference: "the role model is barely
enforced across content" vs "the role checks that exist on management are correct" are both
true. Recorded because a naive read pits `lens-security-authz` against the generalists.

**Evidence.** Enforced (role checked): `workspace_views.py:302,333,390,464,503,554,600`;
`api/views.py:330` (refresh, RW+), `api/views.py:500` (annotate, non-READ);
`transformations/views.py:84,154`. Not enforced (membership only): `knowledge/api/views.py`
(all mutating sites), `recipes/api/views.py` (all sites), `artifacts/views.py:893,915,926`,
chat path (`chat/views.py:109-114`, `user_role` hardcoded "analyst"). The two sets are
disjoint; both descriptions hold.

---

## C7 — Severity of READ-role write access: "bounded within one workspace" vs "cross-tenant exfiltration possible"

**The disagreement.**
- `lens-security-authz` S-1 explicitly caveats: *"impact is bounded to within a single
  workspace's own members and data; there is no cross-tenant escalation here ... Severity is
  'you handed a read-only collaborator write/destroy power', not 'external compromise'."*
- `lens-security-authz` S-3 itself, and `vertical-transformations` F2, describe an authenticated
  tenant member authoring transformation-asset SQL that runs as the managed-DB **superuser** and
  can `SELECT * FROM t_<victim>.raw_cases` into their own schema — i.e. cross-tenant read.

**Resolution: not a contradiction — different surfaces.** S-1's "bounded, no cross-tenant"
caveat is scoped to the *content endpoints* (knowledge/recipes/artifacts/chat) that lack role
checks. S-3 / transformations-F2 concern the *transformations* surface, which the same
security lens flags as its **highest-severity** finding precisely because it *does* allow
cross-tenant reads via unconfined dbt SQL. Both are the same reviewer being internally
consistent across two surfaces. Recorded because "no cross-tenant escalation here" and
"cross-tenant exfiltration possible" sitting in one report reads as self-contradiction until
you note "here" refers only to S-1's content endpoints.

**Evidence.** Content path: `artifacts/views.py:893-932`, `knowledge/api/views.py` — no role,
no superuser, scoped to the viewer's workspace data. Transform path:
`apps/transformations/services/executor.py:134-141` + `mcp_server/services/dbt_runner.py:28-61`
— connects as `MANAGED_DATABASE_URL` (the schema/role-creating superuser), no `SET ROLE`, no
`search_path` confinement, `sql_content` unvalidated. Distinct mechanisms; distinct severities.

---

## C8 — Does the MCP server process have DB-connection hygiene against the June-9 dead-connection class?

**The disagreement.**
- `extra-lens-async-sync-boundary` F1: the MCP server process has **no** Django
  connection hygiene; the 2026-06-09 dead-connection incident class survives there — "every MCP
  tool call that touches the platform DB fails ... until the MCP container is restarted."
- `extra-seam-django-state-vs-procrastinate` "What's fine": "MCP server import order dodges the
  `current_app`-is-a-FutureApp trap ... so its rollback ... gets the real App," and several
  reports list the connection-hygiene decorator as a solved problem.

**Resolution: not a contradiction — they describe two different failure classes in the MCP
process.** The seam report praises the MCP process for avoiding the *FutureApp import-order*
trap (a real, separate issue). The async/sync lens flags that the MCP process lacks
*connection-staleness hygiene* (`close_old_connections` / `CONN_HEALTH_CHECKS`), which is the
dead-connection class. Both are correct; they are not the same mechanism. No report claims the
MCP process *does* have connection hygiene, so there is no genuine clash — recorded only because
"MCP is fine" and "MCP has the June-9 bug" can be misread as opposing.

**Evidence.** `extra-lens-async-sync-boundary` F1 verifies by grep that
`close_old_connections|CONN_MAX_AGE|CONN_HEALTH_CHECKS` appears only in `config/procrastinate.py`
(worker), never in `mcp_server/`. `extra-seam` F4 separately verifies the FutureApp import
timing. Different files, different defects.

---

## Confirmations (consensus claims spot-checked and upheld)

These were not contradictions but were verified because a single report's wording could have
implied an outlier; all check out:

- **`SchemaState.MATERIALIZING` has zero writers.** Grep for any assignment
  (`state = SchemaState.MATERIALIZING`, `.update(state=...MATERIALIZING)`) returns nothing;
  ~15 readers exist (`base.py:213,230,325`, `workspace_views.py:85,249`, `context.py:58`,
  `server.py:694,740`, `workspace_service.py:92,102,107`, etc.). Confirms `generalist`,
  `vertical-materialization` F11, `seam-chat-mcp-worker` F3, `vertical-catalog-dictionary` F4,
  `journey-multi-tenant` F9, `seam-platform-managed-db` F8, `lens-consistency` Family K.

- **`transformation_aware_list_tables` has exactly one production caller** —
  `apps/agents/graph/base.py:249` (prompt assembly); not used by the MCP `list_tables` tool.
  Confirms `vertical-mcp-server` F5 and `vertical-catalog-dictionary` F6 (which agree with each
  other; the apparent "only in mcp_server has no caller" vs "only from prompt assembly in
  apps/agents" phrasings describe the same single call site).

- **`Artifact.create_new_version` has zero callers** — confirms `lens-dead-code` F7. The live
  versioning is inline in `artifact_tool.py`.

- **`extract_oauth_tokens` / the oauth-token transport is dead end-to-end** — multiple reports
  (`generalist-1` F11, `generalist-2`/`-3` F9, `vertical-mcp` F8, `vertical-accounts-auth` F2,
  `vertical-chat-agent` F2, `lens-consistency` Family I, `lens-dead-code` F3) agree; no report
  dissents. (`vertical-accounts-auth` adds the useful nuance that the OCS exclusion in
  `get_user_oauth_tokens` is a red herring because the whole pipe is inert — a refinement, not
  a contradiction.)
