# Frontend Test Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a frontend unit-test runner (Vitest + Testing Library), seed regression tests for the threadId-leak and workspace-switch incident classes, wire frontend unit tests into CI, and make the smoke/e2e suites invocable & documented. (Part of #234, finding 10#5.)

**Architecture:** Vitest runs over the existing Vite/React 19 pipeline in jsdom, driven by `bun run test`. Store and hook regression tests exercise the real zustand store + the `useWorkspaceThreadSync` hook (no network). The smoke suite gets a manual `workflow_dispatch` GitHub workflow; everything invocable is documented in `docs/testing.md`.

**Tech Stack:** Vitest 3, @testing-library/react 16, @testing-library/jest-dom 6, @testing-library/user-event 14, jsdom; bun; GitHub Actions.

---

## File Structure

**Frontend runner (Task 1):**
- Create `frontend/vitest.config.ts` — Vitest config (jsdom, react plugin, `@` alias, setup file). Separate from `vite.config.ts` so build/Sentry plugins don't load under test.
- Create `frontend/src/test/setup.ts` — jest-dom matchers + RTL cleanup wiring.
- Create `frontend/src/test/sanity.test.tsx` — proves the runner + jsdom + matchers work; deleted/kept as a living smoke check (kept).
- Modify `frontend/package.json` — add devDeps + `test`/`test:watch` scripts.
- Modify `frontend/tsconfig.app.json` — add test/runtime types.
- Modify `frontend/tsconfig.node.json` — typecheck `vitest.config.ts`.
- Modify `frontend/eslint.config.js` — ignore `vitest.config.ts`; test-file override.

**Regression tests (Tasks 2–5):**
- Create `frontend/src/components/ChatPanel/threadStorage.test.ts`
- Create `frontend/src/store/domainSlice.test.ts`
- Create `frontend/src/hooks/useWorkspaceThreadSync.test.tsx`
- Create `frontend/src/store/workspaceSwitch.test.ts`

**CI + invocable checks (Tasks 6–7):**
- Modify `.github/workflows/ci.yml` — add a frontend unit-test step.
- Create `.github/workflows/smoke.yml` — manual smoke run.
- Create `docs/testing.md` — every invocable test path.

---

## Task 1: Scaffold the Vitest runner

**Files:**
- Modify: `frontend/package.json`
- Create: `frontend/vitest.config.ts`
- Create: `frontend/src/test/setup.ts`
- Create: `frontend/src/test/sanity.test.tsx`
- Modify: `frontend/tsconfig.app.json`
- Modify: `frontend/tsconfig.node.json`
- Modify: `frontend/eslint.config.js`

- [ ] **Step 1: Install dev dependencies**

Run (from `frontend/`):
```bash
bun add -d vitest @testing-library/react @testing-library/jest-dom @testing-library/user-event jsdom
```
Expected: `package.json` devDependencies gains the five packages; `bun.lock` updates.

- [ ] **Step 2: Add test scripts to `frontend/package.json`**

In the `"scripts"` block, add (keep existing scripts):
```json
    "test": "vitest run",
    "test:watch": "vitest",
```

- [ ] **Step 3: Create `frontend/vitest.config.ts`**

```ts
import { defineConfig } from "vitest/config"
import react from "@vitejs/plugin-react"
import path from "path"

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
})
```

- [ ] **Step 4: Create `frontend/src/test/setup.ts`**

```ts
import { afterEach } from "vitest"
import { cleanup } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"

// Unmount React trees between tests even though globals/auto-cleanup is on —
// explicit and resilient to config changes.
afterEach(() => {
  cleanup()
})
```

- [ ] **Step 5: Write the runner sanity test (failing first)**

Create `frontend/src/test/sanity.test.tsx`:
```tsx
import { describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"

describe("vitest runner", () => {
  it("renders into jsdom and exposes jest-dom matchers", () => {
    render(<div data-testid="probe">ready</div>)
    expect(screen.getByTestId("probe")).toBeInTheDocument()
    expect(screen.getByTestId("probe")).toHaveTextContent("ready")
  })

  it("exposes crypto.randomUUID (used by the store)", () => {
    expect(typeof crypto.randomUUID()).toBe("string")
  })
})
```

- [ ] **Step 6: Run the sanity test**

Run (from `frontend/`): `bun run test`
Expected: 2 tests PASS. If `crypto.randomUUID` is undefined, add to the top of `src/test/setup.ts`:
```ts
import { webcrypto } from "node:crypto"
if (!globalThis.crypto) globalThis.crypto = webcrypto as Crypto
```
(Node 24 normally provides it, so this is a fallback only.)

- [ ] **Step 7: Wire TypeScript so `bun run build` stays green**

In `frontend/tsconfig.app.json`, change the `"types"` line to:
```json
    "types": ["vite/client", "vitest/globals", "@testing-library/jest-dom"],
```

In `frontend/tsconfig.node.json`, change `"include"` to:
```json
  "include": ["vite.config.ts", "vitest.config.ts"]
```

- [ ] **Step 8: Wire ESLint for config + test files**

In `frontend/eslint.config.js`:
- Change the `globalIgnores` line to also ignore the vitest config:
```js
  globalIgnores(['dist', 'vite.config.ts', 'vitest.config.ts']),
```
- Add a new override block at the end of the array (after the `tests/**` block):
```js
  {
    files: ['src/**/*.test.{ts,tsx}', 'src/test/**/*.{ts,tsx}'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
      'react-refresh/only-export-components': 'off',
    },
  },
```

- [ ] **Step 9: Verify lint + build + test all pass**

Run (from `frontend/`):
```bash
bun run lint && bun run build && bun run test
```
Expected: lint clean, `tsc -b && vite build` succeeds, 2 tests pass.

- [ ] **Step 10: Commit**

```bash
git add frontend/package.json frontend/bun.lock frontend/vitest.config.ts \
  frontend/src/test/setup.ts frontend/src/test/sanity.test.tsx \
  frontend/tsconfig.app.json frontend/tsconfig.node.json frontend/eslint.config.js
git commit -m "test(frontend): add Vitest + Testing Library unit-test runner

Part of #234 (frontend test infra / 10#5)."
```

---

## Task 2: Regression test — threadStorage helpers (threadId-leak class)

**Files:**
- Create: `frontend/src/components/ChatPanel/threadStorage.test.ts`
- Reference (do not modify): `frontend/src/components/ChatPanel/threadStorage.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/ChatPanel/threadStorage.test.ts`:
```ts
import { beforeEach, describe, expect, it } from "vitest"
import {
  clearSavedThreadId,
  readSavedThreadId,
  writeSavedThreadId,
} from "./threadStorage"

describe("threadStorage (per-workspace last thread)", () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it("round-trips a saved thread id per workspace", () => {
    writeSavedThreadId("ws-a", "thread-a")
    writeSavedThreadId("ws-b", "thread-b")
    expect(readSavedThreadId("ws-a")).toBe("thread-a")
    expect(readSavedThreadId("ws-b")).toBe("thread-b")
  })

  it("returns null when nothing is saved for a workspace", () => {
    expect(readSavedThreadId("ws-none")).toBeNull()
  })

  it("clearSavedThreadId removes the saved id when it matches", () => {
    writeSavedThreadId("ws-a", "thread-a")
    clearSavedThreadId("ws-a", "thread-a")
    expect(readSavedThreadId("ws-a")).toBeNull()
  })

  it("clearSavedThreadId does NOT clobber a newer saved thread (match guard)", () => {
    // A stale thread's failed load must not wipe a thread saved more recently.
    writeSavedThreadId("ws-a", "thread-new")
    clearSavedThreadId("ws-a", "thread-stale")
    expect(readSavedThreadId("ws-a")).toBe("thread-new")
  })

  it("clearSavedThreadId with no id removes unconditionally", () => {
    writeSavedThreadId("ws-a", "thread-a")
    clearSavedThreadId("ws-a")
    expect(readSavedThreadId("ws-a")).toBeNull()
  })
})
```

- [ ] **Step 2: Run it to confirm it passes (code already exists)**

Run (from `frontend/`): `bun run test src/components/ChatPanel/threadStorage.test.ts`
Expected: 5 tests PASS. (This guards existing behavior from commit `00c423d`; it should pass immediately. If any fail, the test encodes a wrong expectation — fix the test, not `threadStorage.ts`.)

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ChatPanel/threadStorage.test.ts
git commit -m "test(frontend): regression coverage for threadStorage match guard (00c423d)

Part of #234 (frontend test infra / 10#5)."
```

---

## Task 3: Regression test — `setActiveDomain` resets threadId (threadId-leak core)

**Files:**
- Create: `frontend/src/store/domainSlice.test.ts`
- Reference (do not modify): `frontend/src/store/domainSlice.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/store/domainSlice.test.ts`:
```ts
import { beforeEach, describe, expect, it } from "vitest"
import { useAppStore } from "@/store/store"

describe("domainSlice.setActiveDomain — threadId leak guard (00c423d)", () => {
  beforeEach(() => {
    useAppStore.setState({ activeDomainId: "ws-a", threadId: "thread-a" })
  })

  it("resets threadId to a fresh id when switching to a different workspace", () => {
    useAppStore.getState().domainActions.setActiveDomain("ws-b")
    const s = useAppStore.getState()
    expect(s.activeDomainId).toBe("ws-b")
    expect(s.threadId).not.toBe("thread-a")
    // fresh client-generated UUID
    expect(s.threadId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i)
  })

  it("keeps threadId when re-selecting the same workspace", () => {
    useAppStore.getState().domainActions.setActiveDomain("ws-a")
    expect(useAppStore.getState().activeDomainId).toBe("ws-a")
    expect(useAppStore.getState().threadId).toBe("thread-a")
  })
})
```

- [ ] **Step 2: Run it**

Run (from `frontend/`): `bun run test src/store/domainSlice.test.ts`
Expected: 2 tests PASS (guards the existing fix). If the UUID regex fails, confirm jsdom exposes `crypto.randomUUID` (see Task 1 Step 6 fallback).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/store/domainSlice.test.ts
git commit -m "test(frontend): guard setActiveDomain threadId reset (00c423d)

Part of #234 (frontend test infra / 10#5)."
```

---

## Task 4: Regression test — `useWorkspaceThreadSync` does not carry threads across workspaces

**Files:**
- Create: `frontend/src/hooks/useWorkspaceThreadSync.test.tsx`
- Reference (do not modify): `frontend/src/hooks/useWorkspaceThreadSync.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/hooks/useWorkspaceThreadSync.test.tsx`:
```tsx
import { beforeEach, describe, expect, it } from "vitest"
import { act, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { useWorkspaceThreadSync } from "@/hooks/useWorkspaceThreadSync"
import type { TenantMembership } from "@/store/domainSlice"

// Workspace ids with EMPTY names so workspacePath yields the bare
// `/workspaces/<id>` form (no slug) and URLs are fully predictable.
const WS_A = "11111111-1111-1111-1111-111111111111"
const WS_B = "22222222-2222-2222-2222-222222222222"
const THREAD_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

function domain(id: string): TenantMembership {
  return {
    id,
    name: "",
    display_name: "",
    is_auto_created: false,
    role: "manage",
    tenants: [],
    member_count: 1,
    schema_status: "available",
    last_synced_at: null,
    created_at: "2026-01-01T00:00:00Z",
  }
}

function Probe() {
  useWorkspaceThreadSync("")
  const loc = useLocation()
  return <div data-testid="path">{loc.pathname}</div>
}

describe("useWorkspaceThreadSync — no cross-workspace thread carry (00c423d)", () => {
  beforeEach(() => {
    useAppStore.setState({
      domains: [domain(WS_A), domain(WS_B)],
      domainsStatus: "loaded",
      activeDomainId: WS_A,
      threadId: THREAD_A,
    })
  })

  it("navigates to the new workspace with a fresh thread, never grafting the old one", async () => {
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WS_A}/chat/${THREAD_A}`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/chat/:threadId" element={<Probe />} />
          <Route path="/workspaces/:workspaceId/chat" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    )

    // URL → store reconciled; address bar stays on A/threadA.
    await waitFor(() =>
      expect(screen.getByTestId("path").textContent).toBe(
        `/workspaces/${WS_A}/chat/${THREAD_A}`,
      ),
    )

    // Switch workspace the same way the WorkspaceSwitcher does.
    act(() => {
      useAppStore.getState().domainActions.setActiveDomain(WS_B)
    })

    await waitFor(() => {
      const path = screen.getByTestId("path").textContent ?? ""
      expect(path.startsWith(`/workspaces/${WS_B}/chat/`)).toBe(true)
      expect(path).not.toContain(THREAD_A)
    })
  })
})
```

- [ ] **Step 2: Run it**

Run (from `frontend/`): `bun run test src/hooks/useWorkspaceThreadSync.test.tsx`
Expected: 1 test PASS. If a real network call surfaces (it should not — no fetching action runs on this path), add at the top of the file:
```ts
import { vi } from "vitest"
vi.mock("@/api/threads", () => ({ markThreadViewed: vi.fn().mockResolvedValue(undefined) }))
```
and re-run.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/hooks/useWorkspaceThreadSync.test.tsx
git commit -m "test(frontend): guard useWorkspaceThreadSync against cross-workspace thread carry (00c423d)

Part of #234 (frontend test infra / 10#5)."
```

---

## Task 5: Regression test — workspace-switch state reset (coordinates with #247)

**Files:**
- Create: `frontend/src/store/workspaceSwitch.test.ts`

- [ ] **Step 1: Write the test (passing now + #247 placeholders)**

Create `frontend/src/store/workspaceSwitch.test.ts`:
```ts
import { beforeEach, describe, expect, it } from "vitest"
import { useAppStore } from "@/store/store"

/**
 * Workspace-switch state-reset contract. Coordinates with issue #247.
 *
 * PASSING NOW: switching workspaces starts a fresh per-workspace thread — the
 * only per-workspace state reset that exists today (from the 00c423d
 * threadId-leak fix).
 *
 * SKIPPED (#247): on a workspace switch the pages must also refetch / clear
 * their per-workspace state. #247 supplies the runtime fix; when it lands, flip
 * the `it.skip` calls below to `it` and assert the refetch/clear behaviour.
 */
describe("workspace switch resets per-workspace state", () => {
  beforeEach(() => {
    useAppStore.setState({ activeDomainId: "ws-a", threadId: "thread-a" })
  })

  it("starts a fresh thread for the new workspace (no cross-workspace carry)", () => {
    const before = useAppStore.getState().threadId
    useAppStore.getState().domainActions.setActiveDomain("ws-b")
    expect(useAppStore.getState().threadId).not.toBe(before)
  })

  // --- #247: pages don't refetch / clear state on workspace switch ---
  it.skip("refetches the recipes list on workspace switch (#247, 04#9)", () => {})
  it.skip("refetches the artifacts list on workspace switch (#247, 04#9)", () => {})
  it.skip("clears a prior workspace-detail load error on later success (#247, 05#5)", () => {})
})
```

- [ ] **Step 2: Run it**

Run (from `frontend/`): `bun run test src/store/workspaceSwitch.test.ts`
Expected: 1 PASS, 3 SKIPPED.

- [ ] **Step 3: Run the whole suite**

Run (from `frontend/`): `bun run test`
Expected: all tests pass (sanity + threadStorage + domainSlice + hook + workspaceSwitch), 3 skipped, 0 failures.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/store/workspaceSwitch.test.ts
git commit -m "test(frontend): workspace-switch state-reset contract + #247 placeholders

Part of #234 (frontend test infra / 10#5)."
```

---

## Task 6: Wire frontend unit tests into CI

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Add the unit-test step**

In `.github/workflows/ci.yml`, in the `lint-frontend` job, after the existing
"Lint frontend" step, append:
```yaml
      - name: Unit test frontend
        run: bun run test
        working-directory: frontend
```
(Keep the job named `lint-frontend` so existing required-status-check config is
undisturbed.)

- [ ] **Step 2: Validate the workflow YAML**

Run (from repo root):
```bash
uv run python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml')); print('ci.yml OK')"
```
Expected: `ci.yml OK`.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run frontend unit tests in the frontend job

Part of #234 (frontend test infra / 10#5)."
```

---

## Task 7: Make smoke (and document e2e) invocable

**Files:**
- Create: `.github/workflows/smoke.yml`
- Create: `docs/testing.md`

- [ ] **Step 1: Create the manual smoke workflow**

Create `.github/workflows/smoke.yml`:
```yaml
name: Smoke Tests (manual)

# Post-deploy smoke checks against a running Scout deployment. Manual only —
# needs a live URL + (optionally) credentials, so it never runs in PR CI.
on:
  workflow_dispatch:
    inputs:
      frontend_url:
        description: "Frontend base URL (SCOUT_FRONTEND_URL)"
        required: true
        default: "https://labs.connect.dimagi.com/scout"
      api_url:
        description: "API base URL (SCOUT_API_URL)"
        required: true
        default: "https://labs.connect.dimagi.com/scout"

jobs:
  smoke:
    runs-on: ubuntu-latest
    env:
      SCOUT_FRONTEND_URL: ${{ inputs.frontend_url }}
      SCOUT_API_URL: ${{ inputs.api_url }}
      # Optional — the authenticated-flow tests skip when these are absent.
      SCOUT_TEST_EMAIL: ${{ secrets.SCOUT_TEST_EMAIL }}
      SCOUT_TEST_PASSWORD: ${{ secrets.SCOUT_TEST_PASSWORD }}
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v6

      - name: Run deployment smoke tests
        run: >-
          uv run pytest tests/smoke/test_deployment.py
          -v --override-ini="addopts=" -p no:django
```

- [ ] **Step 2: Validate the workflow YAML**

Run (from repo root):
```bash
uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/smoke.yml')); print('smoke.yml OK')"
```
Expected: `smoke.yml OK`.

- [ ] **Step 3: Verify the smoke command collects offline**

Run (from repo root):
```bash
uv run pytest tests/smoke/test_deployment.py --collect-only --override-ini="addopts=" -p no:django -q | tail -3
```
Expected: tests collected, no import/collection errors.

- [ ] **Step 4: Create `docs/testing.md`**

```markdown
# Testing

Every runnable test path in Scout.

## Backend (Python / pytest)

```bash
uv run pytest                 # full suite (smoke tests excluded by default)
uv run pytest tests/test_x.py # one file
uv run pytest -k name         # by name
```

The default `addopts` (`pyproject.toml`) include `-m 'not smoke'`, so smoke tests
are excluded from the normal run. CI runs `uv run pytest` via
`.github/workflows/test.yml`.

## Frontend unit tests (Vitest + Testing Library)

```bash
cd frontend
bun run test        # one-shot (used by CI)
bun run test:watch  # watch mode for local dev
```

Runs in jsdom. Config: `frontend/vitest.config.ts`; setup:
`frontend/src/test/setup.ts`. CI runs `bun run test` in the `lint-frontend` job
(`.github/workflows/ci.yml`).

## Smoke tests (post-deploy, live deployment)

HTTP checks against a running deployment. Excluded from the default backend run;
run explicitly:

```bash
uv run pytest tests/smoke/test_deployment.py \
    -v --override-ini="addopts=" -p no:django \
    # configure target via env or tests/smoke/.env:
    #   SCOUT_FRONTEND_URL=... SCOUT_API_URL=...
    #   SCOUT_TEST_EMAIL=... SCOUT_TEST_PASSWORD=...  (authenticated-flow tests)
```

In CI: run the **Smoke Tests (manual)** workflow
(`.github/workflows/smoke.yml`) via *Actions → Run workflow*, supplying the
target URLs. `test_connect_sync.py` additionally needs platform-DB access — see
`tests/smoke/conftest.py` and `tests/smoke/.env.example`.

## Frontend end-to-end (Playwright)

```bash
cd frontend
bun run test:e2e              # all projects
bun run test:e2e:widget       # widget-sdk project only
bun run test:e2e:integration  # embed-integration (auto-starts API + Vite)
```

Config: `frontend/playwright.config.ts`. Some projects require a live backend
and/or credentials. Fully gated, credentialed e2e in CI is a planned follow-up
(see #234).
```

(Note: the fenced code blocks above are part of the file content — write them verbatim.)

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/smoke.yml docs/testing.md
git commit -m "ci: add manual smoke workflow + document all test paths

Part of #234 (frontend test infra / 10#5)."
```

---

## Task 8: Final verification & PR

- [ ] **Step 1: Full local verification**

Run (from `frontend/`):
```bash
bun run lint && bun run build && bun run test
```
Expected: lint clean; `tsc -b && vite build` succeeds; all unit tests pass (3 skipped), 0 failures.

- [ ] **Step 2: Confirm scope — only intended files changed**

Run (from repo root): `git diff --stat origin/main...HEAD`
Expected: only the files listed in this plan (frontend runner + tests, `ci.yml`, `smoke.yml`, `docs/testing.md`, the two spec/plan docs). No backend source changes, no `#247` runtime changes.

- [ ] **Step 3: Push branch and open the PR**

Use the dev-utils:create-pr skill (or `gh pr create`). PR title references #234
(frontend test infra). PR body MUST contain **"Part of #234 (frontend test infra
/ 10#5)"** and MUST NOT use "Closes #234" (backend findings remain).

- [ ] **Step 4: Iterate against the auto-review until clean, then add reviewer**

Address auto-review feedback (use superpowers:receiving-code-review for judgment).
Once the PR is clean, add `snopoke` as a reviewer.

---

## Self-Review (author check)

- **Spec coverage:** runner (Task 1) ✓; threadId-leak regression — threadStorage
  (Task 2), setActiveDomain (Task 3), hook (Task 4) ✓; workspace-switch reset +
  #247 placeholders (Task 5) ✓; CI unit-test step (Task 6) ✓; smoke invocable +
  e2e documented (Task 7) ✓; PR conventions (Task 8) ✓.
- **Placeholder scan:** no TBD/TODO; all code shown in full.
- **Type consistency:** `setActiveDomain`, `threadId`, `activeDomainId`,
  `domains`, `domainsStatus`, `TenantMembership`, `readSavedThreadId`,
  `writeSavedThreadId`, `clearSavedThreadId`, `useWorkspaceThreadSync(pathPrefix)`
  all match the current source signatures verified during planning.
- **Out of scope (untouched):** backend 12#0 / 07#0 / 02#6, contract test 10#4
  (PR #274), and #247's runtime fix.
```
