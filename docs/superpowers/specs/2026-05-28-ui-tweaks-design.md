# Scout UI Tweaks — Design

**Date:** 2026-05-28
**Branch:** `sk/ui-tweaks`
**Status:** draft

## Goal

Three discrete UI changes to make Scout easier to use, all driven from a mockup:

1. **Welcome + starter questions** in the chat empty state, tailored to the active workspace's tenant provider (CommCare HQ / CommCare Connect / OCS).
2. **Workspace badge in the top-right** so users can always see which workspace they're operating in — visible on every page, not just chat.
3. **Prominent chat bar** in the empty state with a safety line and (when known) a data-freshness timestamp.

## Non-goals

- Recipe / Knowledge chips inside the chat input — the existing slash-command menu (`/recipe`, `/knowledge`) already covers this.
- Editing starter questions without a code deploy.
- Animated transitions between empty-state and compact-input layouts.
- A general top-header system (page titles, breadcrumbs). The new top bar has only a workspace badge on the right; the left half stays empty.

## Architecture

Three new frontend components, one backend serializer change.

```
AppLayout (changed: adds top bar slot above main content)
└── TopBar (new) — slim header, right-aligned WorkspaceBadge, hosts chat Share button on chat routes
└── WorkspaceBadge (new) — provider icon + display_name; tenant list subtext when multi-tenant
└── ChatPanel (changed: branch on messages.length, drops its own header strip)
    ├── ChatEmptyState (new) — welcome heading + starter cards + prominent input + freshness line
    │   └── starterQuestions.ts (new) — provider → string[3]
    └── (existing compact input layout) — unchanged for non-empty threads
```

`ChatEmptyState` does not own chat state; `ChatPanel` keeps `input`, `setInput`, `sendMessage`, `status`, and the slash-command menu, and passes the handlers in as props. This avoids duplicating form/slash logic across the two layouts.

## Component design

### TopBar

- Lives inside `AppLayout`, in the right column above `<Outlet />` and above `<ArtifactPanel />`.
- Height ~44px, bottom border (`border-b`), transparent background.
- Right-aligned slot for `<WorkspaceBadge />`.
- On chat routes, also renders the `Share` button (moved out of `ChatPanel`).
- Left half is empty for now; reserved for future page-title / breadcrumb use.

```
┌────────┬───────────────────────────────┬──────────┐
│        │  TopBar       [WorkspaceBadge]│          │
│ Sidebar├───────────────────────────────┤ Artifact │
│        │  <Outlet />                   │ Panel    │
└────────┴───────────────────────────────┴──────────┘
```

The Share button moves into the TopBar to avoid two stacked header strips on chat routes. It's only rendered when the current route is a chat route AND the active thread has messages — same visibility rule as today.

### WorkspaceBadge

- Pill-shaped non-interactive `<div>` (no chevron — explicitly not a selector; the sidebar selector remains the way to switch workspaces).
- Reads `activeDomainId` from the Zustand store, looks up the workspace in `state.domains`.
- Renders:
  - **Provider icon** from a `providerMeta` map: `commcare`, `commcare_connect`, `ocs`, plus a generic database icon for unknown.
  - **Workspace `display_name`** (already provider-formatted by backend — e.g. `[CC] Hello World`).
  - **Tenant subtext** (small, muted) listing tenant `tenant_name`s comma-separated, truncated. Only rendered when `tenants.length > 1`.
- `data-testid="workspace-badge"`, plus `data-provider="<provider>"` for QA targeting.
- Provider icons: new SVGs under `frontend/src/assets/providers/{commcare,commcare-connect,ocs}.svg`. (I'll check `frontend/src/assets/` first and reuse anything already there.)

### ChatEmptyState

Triggered inside `ChatPanel` when `messages.length === 0` and not streaming. Renders inside the chat pane, vertically and horizontally centered in a `max-w-3xl mx-auto` container. The page-level full-width rule still holds because the chat pane *is* the full-width page content; the centering is internal to the empty state.

Layout:

```
              I'm Scout! Your AI-powered Data Analyst.
              How can I assist you today?

   ┌────────────────┐ ┌────────────────┐ ┌────────────────┐
   │ Starter Q 1  → │ │ Starter Q 2  → │ │ Starter Q 3  → │
   └────────────────┘ └────────────────┘ └────────────────┘

   ┌────────────────────────────────────────────────────┐
   │  Ask about your data...                            │
   │                                                  ↑ │
   └────────────────────────────────────────────────────┘
   Scout can only read your data — never modify or delete it.
   [Data last synced 12 minutes ago.]
```

- **Heading**: `text-2xl`/`text-3xl`, two lines.
- **Starter cards**: 3-column grid (collapses to 1 column below `md`). Each card uses existing card / border tokens. Click → calls `props.onSubmit(question)` which calls `sendMessage({ text: question })` — sends immediately, no edit step.
- **Prominent input**: `<textarea>` styled as a card with a send button absolutely positioned in the bottom-right corner. Enter submits, Shift+Enter newline. Distinct from the compact `<Input>` used mid-chat. Shares `input`/`setInput`/slash-menu state with `ChatPanel`.
- **Freshness subtext**: always renders the safety line. If `workspace.last_synced_at` is set, appends `Data last synced {relativeTime(last_synced_at)}.`
- `data-testid`s: `chat-empty-state`, `starter-question-0|1|2`, `chat-input-prominent`, `data-freshness`.

Provider lookup: `workspace.tenants[0]?.provider`. If missing or unknown, falls through to the `default` starter set.

### starterQuestions.ts

```ts
type Provider = "commcare" | "commcare_connect" | "ocs" | "default"

export const STARTER_QUESTIONS: Record<Provider, string[]> = {
  commcare: [
    "How many cases were opened last month?",
    "Which mobile workers haven't submitted a form in the last week?",
    "Compare form submission volume this quarter vs last quarter.",
  ],
  commcare_connect: [
    "How many workers completed verified visits this week?",
    "Which opportunities have the highest payment volume this month?",
    "Compare average payment per worker across opportunities.",
  ],
  ocs: [
    "How many conversations did the bot handle in the last 7 days?",
    "What are the most common user messages this week?",
    "Compare session counts across bots this month vs last month.",
  ],
  default: [
    "What tables are available in my data?",
    "Show me a high-level overview of the schema.",
    "What are the most recently updated records?",
  ],
}
```

The CommCare three come from the mockup. CommCare Connect / OCS are reasonable starters informed by each product's domain (payments/opportunities for Connect; conversations/bots for OCS) — they exist to be replaced by anyone with deeper product knowledge during code review.

## Backend: freshness

Add `last_synced_at: datetime | null` to the workspace list and detail responses in `apps/workspaces/api/views.py`.

**Definition.** The `completed_at` of the most recent `MaterializationRun` whose `state == COMPLETED` and whose `tenant_schema.tenant_id` is in the workspace's tenants. `null` if no completed run exists.

**Implementation.** A single annotated subquery on the workspace queryset, returning `MAX(completed_at)` across the workspace's tenants. Conceptually:

```python
latest_run = MaterializationRun.objects.filter(
    state=MaterializationRun.RunState.COMPLETED,
    tenant_schema__tenant__in=OuterRef("tenants"),
).order_by("-completed_at").values("completed_at")[:1]

queryset.annotate(last_synced_at=Subquery(latest_run))
```

Avoids N+1 on the list endpoint. Per-workspace tenant counts are typically small, and list pages return <100 workspaces.

**Trade-off considered**: a separate `/api/workspaces/{id}/freshness/` endpoint. Rejected — every page wanting freshness would need a second fetch, and the payload is one timestamp.

## Frontend types

In `frontend/src/api/workspaces.ts`:

```ts
interface WorkspaceListItem {
  ...
  last_synced_at: string | null   // ISO-8601, new
}

interface WorkspaceDetail {
  ...
  last_synced_at: string | null   // ISO-8601, new
}
```

The store's `domains` slice picks up the new field for free; no slice changes required.

## Helpers

`frontend/src/lib/relativeTime.ts`:

```ts
export function formatRelativeTime(iso: string): string
```

Uses `Intl.RelativeTimeFormat`, bucketing to seconds/minutes/hours/days. Returns strings like `"12 minutes ago"`, `"2 hours ago"`. Only consumer for now is `ChatEmptyState`'s freshness line, but it's general-purpose.

## Data flow

1. App load → `domainSlice.fetchDomains()` → `GET /api/workspaces/` → each workspace now has `last_synced_at`.
2. `WorkspaceBadge` reads `state.domains.find(d => d.id === activeDomainId)` and renders.
3. `ChatPanel` mounts. If `messages.length === 0`: renders `<ChatEmptyState workspace={activeWorkspace} input={input} setInput={setInput} onSubmit={onSubmit} />`. Otherwise renders the existing compact layout.
4. User clicks a starter card → `onSubmit(question)` → `sendMessage({ text: question })` → first message lands → re-render → compact layout.

## Testing

### Frontend

No new unit-test infrastructure. Coverage via:

- **QA scenarios** in `tests/qa/` — new `data-testid`s (`workspace-badge`, `chat-empty-state`, `starter-question-0|1|2`, `chat-input-prominent`, `data-freshness`) let showboat/rodney target the new elements. Scenarios likely needing updates: empty-state assertions, starter-click flow, badge presence on non-chat pages, freshness presence when sync data exists.
- **Manual smoke** before merge:
  - Empty state renders with welcome + 3 starters + prominent input.
  - Clicking a starter immediately sends and transitions to compact layout.
  - Badge shows correct provider icon + name on Chat / Knowledge / Recipes / Connections / Data Dictionary.
  - Multi-tenant workspace shows tenant subtext.
  - Freshness line shows the safety message always, and appends the timestamp only when `last_synced_at` is set.

### Backend

One pytest in `apps/workspaces/tests/` (existing API test file if there's a natural home; new file otherwise). Cases:

1. `last_synced_at` is `null` when no completed `MaterializationRun` exists.
2. Returns the latest completed run's `completed_at` when one exists.
3. Ignores in-flight / failed runs.
4. On a multi-tenant workspace, returns the max `completed_at` across all tenants.

Sync DRF endpoint — standard `pytest-django` + DRF test client, no async client needed.

## File-by-file change list

**New files**
- `frontend/src/components/TopBar/TopBar.tsx`
- `frontend/src/components/TopBar/index.ts`
- `frontend/src/components/WorkspaceBadge/WorkspaceBadge.tsx`
- `frontend/src/components/WorkspaceBadge/index.ts`
- `frontend/src/components/WorkspaceBadge/providerMeta.ts` (icon + label map)
- `frontend/src/components/ChatEmptyState/ChatEmptyState.tsx`
- `frontend/src/components/ChatEmptyState/starterQuestions.ts`
- `frontend/src/components/ChatEmptyState/index.ts`
- `frontend/src/lib/relativeTime.ts`
- `frontend/src/assets/providers/commcare.svg` (and connect / ocs, if no existing assets)

**Changed files**
- `frontend/src/components/AppLayout/AppLayout.tsx` — wrap main column with TopBar
- `frontend/src/components/ChatPanel/ChatPanel.tsx` — branch on `messages.length`, drop own header strip (move Share into TopBar), pass handlers to `ChatEmptyState`
- `frontend/src/api/workspaces.ts` — add `last_synced_at: string | null` to list and detail types
- `apps/workspaces/api/views.py` — annotate workspace queryset with `last_synced_at`; include in serialized output
- `apps/workspaces/tests/...` — new test cases for `last_synced_at`

## Risks & mitigations

- **Subquery cost on workspace list.** One correlated subquery per workspace row. Acceptable for typical user list sizes (<100). If profile shows it as a regression, fold into a single GROUP BY query.
- **Provider mismatch on multi-tenant workspaces.** A workspace can span multiple tenants of different providers. Using `tenants[0]?.provider` picks the first. Acceptable: it matches how `display_name` already resolves provider, and the tenant subtext makes the multi-tenant nature visible.
- **Empty-state flicker on first send.** When `messages.length` flips from 0 to 1, the layout swaps instantly. Verify the textarea's focus and any in-flight slash-command state don't break the transition.
- **Top bar layout shift on pages that previously had no chrome.** The 44px addition is consistent across all pages, so no individual page is uniquely affected. Verify scrollable pages (`ConnectionsPage`, `KnowledgePage`) still position content correctly under the bar.

## Open questions

None — all design decisions resolved during brainstorming.
