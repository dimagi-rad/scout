# Frontend test infrastructure — design

**Date:** 2026-06-16
**Issue:** Part of #234 (arch-review Wave 0), finding **10#5** only.
**Scope owner:** frontend test infra. The backend contract test (10#4) shipped in
PR #274 and is **not** revisited here.

## Problem

Per arch-review finding 10#5:

- `frontend/package.json` has **no unit-test runner** — only Playwright e2e
  scripts that never run in CI (the frontend CI job is ESLint only).
- The Playwright e2e suite (`frontend/playwright.config.ts`) **runs in no
  workflow**.
- The post-deploy smoke suite (`tests/smoke/test_deployment.py`) is **excluded by
  default** (`pyproject.toml` `addopts = "... -m 'not smoke'"`) and **no workflow
  invokes it**. Both June 2026 incidents were caught by humans noticing a stuck
  UI, not by automation.
- Two known incident classes have **zero regression coverage**:
  - the threadId-leak fix (commit `00c423d`, `useWorkspaceThreadSync.ts` +
    `domainSlice.setActiveDomain` + `threadStorage.ts`),
  - workspace switching not resetting per-workspace state.

## Goals

1. A frontend unit-test runner (Vitest + Testing Library), runnable via `bun`.
2. Seed regression tests for the two incident classes above.
3. A frontend unit-test step in CI.
4. Make the existing-but-unrun checks (smoke pytest, Playwright e2e) invocable and
   documented.

## Non-goals (explicitly out of scope — leave on #234)

- Backend mock-audit findings **12#0** and **07#0**.
- Prompt-vs-tool drift **02#6**.
- The backend chat↔MCP contract test **10#4** (already shipped, PR #274).
- The **runtime fix** for workspace-switch refetch — that is **#247**. This work
  builds the test harness and a passing-now regression test; #247 supplies the
  runtime fix and flips the skipped placeholders to live assertions.
- Full gated/credentialed Playwright e2e in CI — explicit follow-up.

## Design

### 1. Unit-test runner — Vitest + Testing Library

Chosen because it is the native fit for the existing Vite / React 19 / bun stack
(shares Vite's transform pipeline; no separate Babel/Jest config) and the task
mandates it.

- **devDependencies** added to `frontend/package.json`: `vitest`,
  `@testing-library/react`, `@testing-library/jest-dom`,
  `@testing-library/user-event`, `jsdom`.
- **`frontend/vitest.config.ts`** — kept separate from `vite.config.ts` so the
  Sentry plugin / build (`sourcemap`, `base`) config does **not** load under test.
  Uses `defineConfig` from `vitest/config` with:
  - `@vitejs/plugin-react`
  - `resolve.alias` `@` → `./src` (mirrors `vite.config.ts`)
  - `test.environment = "jsdom"`
  - `test.globals = true` (so `describe/it/expect` are global; pairs with jest-dom)
  - `test.setupFiles = ["./src/test/setup.ts"]`
- **`frontend/src/test/setup.ts`** — imports `@testing-library/jest-dom/vitest`;
  RTL auto-cleanup is on by default when `globals: true`.
- **`package.json` scripts**:
  - `"test": "vitest run"` — one-shot, used by CI.
  - `"test:watch": "vitest"` — local dev.
- **TypeScript** — `bun run build` runs `tsc -b` and must keep passing. Test files
  reference Vitest globals + jest-dom matchers. Plan: add the Vitest/jest-dom
  types so test files typecheck, while keeping `*.test.ts(x)` and the `src/test/`
  setup **out of the production build typecheck**. Exact tsconfig shape (a
  dedicated `tsconfig.vitest.json` in the project-references graph vs. a `types`
  entry + `exclude`) is settled during TDD against the current
  `tsconfig*.json` layout; the invariant is **`bun run build` stays green**.

### 2. Seed regression tests

#### threadId-leak class (commit `00c423d`)

- **`src/components/ChatPanel/threadStorage.test.ts`** — pure localStorage helpers
  (`read/write/clearSavedThreadId`). Key case: `clearSavedThreadId`'s match-guard
  only removes the saved id when it matches the passed id (so a stale thread's
  failed load can't clobber a newer saved thread).
- **`src/store/domainSlice.test.ts`** — `setActiveDomain`:
  - switching to a **different** workspace resets `threadId` to a fresh UUID
    (the core leak fix),
  - re-selecting the **same** workspace leaves `threadId` unchanged.
- **`src/hooks/useWorkspaceThreadSync.test.tsx`** — hook test (file named in the
  task) via `renderHook` + `MemoryRouter`: after a workspace switch the hook
  navigates to the **new** workspace's chat URL carrying the **new** thread, never
  grafting the previous workspace's thread id onto the new URL.

#### workspace-switch state reset (coordinating with #247)

- **Passing-now** assertion: `setActiveDomain` resets the per-workspace `threadId`
  on switch (the only per-workspace reset that exists today). Lives in the
  `domainSlice` test above and/or a dedicated `workspaceSwitch.test.ts`.
- **`it.skip(...)` placeholders** referencing #247 for the still-broken gaps so the
  harness is ready and the gap is visible without breaking CI:
  - recipes list refetches on workspace switch (#247, 04#9),
  - artifacts list refetches on workspace switch (#247, 04#9),
  - workspace-detail page clears a prior load error on a later success (#247, 05#5).

  #247's PR converts these from `skip` to live assertions once its runtime fix
  lands.

### 3. CI

- Add a **"Unit test frontend"** step (`bun run test`, `working-directory:
  frontend`) to the existing `lint-frontend` job in `.github/workflows/ci.yml`,
  after the lint step. Reuses the same `bun install`; keeping the job name avoids
  disturbing any branch-protection required-status-check config.

### 4. Make existing checks invocable

- **`.github/workflows/smoke.yml`** — `workflow_dispatch` only. Inputs for the
  target frontend/API URL; credentials via inputs/secrets. Runs the smoke suite:
  `uv run pytest tests/smoke -m smoke --override-ini="addopts=" -p no:django -v`.
  A real invocable CI path that does **not** touch `deploy.yml` / `deploy-labs.yml`.
- **`docs/testing.md`** — documents every invocable test path: frontend unit
  (`bun run test`), backend (`uv run pytest`), smoke (manual + the dispatch
  workflow, with the exact `--override-ini` invocation), and Playwright e2e
  (`bun run test:e2e` and the per-project variants). Notes that fully gated
  credentialed e2e in CI is a follow-up.

## Testing approach

TDD per unit: write the failing test first, then the minimal config/code to pass.
For the runner itself, the first "test" is a trivial smoke spec proving the runner
+ jsdom + jest-dom matchers work; then the real regression specs. Final
verification: `bun run test`, `bun run lint`, and `bun run build` all green in the
worktree before opening the PR.

## Risks / notes

- `crypto.randomUUID()` is used by `setActiveDomain`/`uiSlice`; confirm jsdom
  exposes it under Node 24 (it does via `globalThis.crypto`) during the first
  store test.
- The hook test must avoid navigation ping-pong flakiness — assert on a spied
  `navigate` (or resulting store/URL state) deterministically, not on timing.
- Keep the skipped #247 placeholders clearly commented with the issue + finding id
  so the handoff is unambiguous.

## PR

One branch, one PR titled for #234 (frontend test infra). Body: **"Part of #234
(frontend test infra / 10#5)"** — not "Closes #234", since backend findings
remain. Iterate against the auto-review until clean, then add `snopoke` as
reviewer.
