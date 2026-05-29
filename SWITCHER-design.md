# Workspace Switcher Redesign

## Problem

The current switcher (`frontend/src/components/Sidebar/Sidebar.tsx`, lines 78–154) is a
`w-56` popover with a search box and a single `max-h-60 overflow-y-auto` list of **all**
workspaces. For typical users that's ~270 workspaces (135 `commcare_connect` + 139 `ocs`),
which produces a tiny scrollbar that's painful to navigate. Two concrete gaps:

1. **No quick access to recently used workspaces** — every switch means scrolling or typing.
2. **No way to tell which workspaces already have data** (are set up / materialized) vs. empty
   ones.

### Data available (no backend change needed)

`GET /api/workspaces/` (`apps/workspaces/api/workspace_views.py::WorkspaceListView`) returns
`WorkspaceListItem` with:

- `display_name`, `id`, `role`, `created_at`
- `tenants: [{ id, tenant_name, provider }]` — provider drives icon/label
  (`commcare`, `commcare_connect`, `ocs`) via `WorkspaceBadge/providerMeta.tsx`.
- **`last_synced_at: string | null`** — populated **only** when a `MaterializationRun` has
  `COMPLETED` for one of the workspace's tenants. This is the authoritative **"has data"**
  signal: non-null ⇒ the workspace has been materialized at least once.

"Recent" has no backend support, so it must be tracked client-side (localStorage), keyed by
workspace id with a timestamp, written whenever a workspace is activated.

---

## Proposal A — "Recents + Search-first" minimal dropdown

**Layout.** Keep the existing `Popover` anchored to the sidebar trigger, widen to `w-72`.
Top: search input (autofocus on open). Below, two labeled sections in one scroll container:

- **Recent** (up to 5, from localStorage, most-recent-first) — shown only when search is empty.
- **All workspaces** (alphabetical) — virtualized so 270 rows render cheaply.

Each row: provider icon, name (truncate), and a small "has data" dot/label derived from
`last_synced_at`. Active workspace gets a check.

**Interaction.** Type to filter across the whole list (recents hidden while searching).
Enter selects the top match. Footer keeps "Manage workspaces" / "New workspace".

**Data/indicators.** `last_synced_at != null` ⇒ filled dot + "Active data"; else hollow dot +
"No data yet". localStorage recents.

**Tradeoffs.** Lowest-risk, closest to current UX. Virtualization needed for 270 rows but adds
a dependency or hand-rolled windowing. No provider segmentation, so a search for a common term
still returns a long mixed list.

**Reuses.** `Popover`, `Input`, `providerMeta`, existing footer.

---

## Proposal B — Command-palette / fuzzy overlay (⌘K)

**Layout.** A centered modal overlay (Dialog) opened from the sidebar trigger **and** a global
`⌘K`/`Ctrl-K` shortcut. Single large search field; results below as a flat, keyboard-navigable
list with provider icon + "has data" badge. A "Recent" group shows when the query is empty.

**Interaction.** Fully keyboard-driven: arrow keys move, Enter selects, Esc closes. Fuzzy match
on name + tenant names + provider label. Results capped (e.g. 50) with "refine your search"
hint when more match.

**Data/indicators.** Same `last_synced_at` signal; recents from localStorage. Could add a
"Has data" toggle filter chip.

**Tradeoffs.** Best raw navigation speed for power users and scales past 270 trivially (capped
results, no virtualization). But it's a heavier interaction model, takes over the screen, and
diverges from the in-sidebar feel the rest of Scout uses. Discoverability of the indicator is
lower (badges in a dense list). More code (global shortcut, focus trap, fuzzy lib or hand-rolled
scorer).

**Reuses.** `Dialog`, `Input`, `providerMeta`. Would benefit from `cmdk` (not installed).

---

## Proposal C — Sectioned panel with provider tabs + Recents + "has data" filter

**Layout.** Wider popover (`w-80`). Top: search. A compact **tab strip** (reusing the app's
underline `Tabs` style) across providers present in the user's set: `All · Recent · CommCare
Connect · Open Chat Studio`. Below the tabs, a **"Has data only"** toggle chip. Then the
filtered, virtualized list with provider icon + clear data indicator and member/role context.

**Interaction.** Tabs slice the list to one provider (or Recent); the toggle hides empty
workspaces; search narrows within the active slice. This directly tames the "135 + 139" split
by letting users jump to their provider first.

**Data/indicators.** `last_synced_at` for the data dot **and** the "Has data only" filter;
provider counts in tabs; recents from localStorage.

**Tradeoffs.** Most powerful for hundreds of mixed-provider workspaces and most legible "has
data" story. Slightly busier; tabs + toggle + search is more chrome than a minimal dropdown, and
it's the most code. Risk of feeling heavy if a user only has a handful of workspaces (must
degrade gracefully — hide tabs/toggle when not useful).

**Reuses.** `Popover`, `Tabs` (underline style), `Input`, `providerMeta`, the
`SearchFilterBar` chip pattern.

---

## Chosen design (synthesis): "Recents-first sectioned panel"

Pick **C as the backbone**, fold in **A's recents-first default** and **B's keyboard select**,
and **drop B's full-screen takeover** — it breaks the in-sidebar cohesion Scout values, and the
sidebar popover is the natural home. Rationale:

- The user explicitly wants (1) a **recents toggle/section** and (2) a **has-data indicator**,
  plus pleasant navigation at ~270. C's provider tabs + has-data filter are the only direction
  that structurally tames a 135/139 two-provider split; A alone leaves a long mixed list, and B
  trades cohesion for speed.
- Honesty (per project memory): the data indicator must reflect a **real** signal. We use
  `last_synced_at != null` and label it plainly; we never imply data exists when it doesn't.

### Final spec — `WorkspaceSwitcher` component

Extracted from `Sidebar.tsx` into
`frontend/src/components/WorkspaceSwitcher/WorkspaceSwitcher.tsx`.

**Trigger.** Unchanged button (`data-testid="domain-selector"`) showing active name +
provider icon, chevron.

**Panel** (`w-80` popover, anchored `align="start"`):

1. **Search** (autofocus): filters by workspace name + tenant names. `data-testid="workspace-search"`.
2. **Segment row** — only rendered when there are >1 provider OR >12 workspaces (degrades for
   small accounts):
   - **Recent** | **All** | one chip per provider present (`CommCare Connect`, `Open Chat
     Studio`, …) with counts. Underline-tab visual language. `data-testid="workspace-seg-{key}"`.
   - **Has data** toggle chip on the right (`data-testid="workspace-filter-hasdata"`), filters to
     `last_synced_at != null`.
3. **List** (`max-h-72`, hand-rolled windowing for >60 visible rows to keep 270 smooth — no new
   dependency):
   - **Recent** segment (default when search empty): up to 8 most-recent from localStorage.
   - Otherwise alphabetical within the active provider slice.
   - **Row** (`data-testid="domain-item-{id}"`): provider icon · name (truncate) · **data dot**
     (filled emerald = has data, hollow muted = none) with `title`/`aria-label` "Has data,
     last synced …" / "No data yet". Active row: subtle `bg-accent` + check.
   - Keyboard: ↑/↓ move highlight, Enter selects highlighted (or top match), Esc closes.
   - Empty state: "No workspaces match." / for Has-data filter: "No workspaces with data yet."
4. **Footer** (unchanged actions): "Manage workspaces", "New workspace".

**Recents tracking.** New `lib/recentWorkspaces.ts`: `getRecentWorkspaceIds()`,
`recordWorkspaceUse(id)` (localStorage, capped at 12, deduped, newest-first). Called from the
switcher's select handler. Resilient to unavailable storage.

**Indicators source of truth.** `hasData(ws) = ws.last_synced_at != null`. Single helper so the
switcher and any future surface agree.

**Why this is pleasant at 270.** Default view shows only ~8 recents (no scrolling for the common
case). When a user does browse, provider tabs cut the list to ~135 max, search narrows further,
and the "Has data" toggle surfaces the handful that are actually set up. Windowing keeps render
cost flat regardless of list size — strictly better than the old tiny scrollbar.
