import { test, expect } from "@playwright/test";

/**
 * Workspace-switch refetch coverage for ArtifactsPage (arch #247).
 *
 * Regression guard: ArtifactsPage fetched its list only on mount/search-change,
 * with an effect dependency array that excluded `activeDomainId`. Switching the
 * active workspace left the PREVIOUS workspace's artifacts on screen, which then
 * 404'd when opened (the new `activeDomainId` combined with a stale artifact id).
 * The fix adds `activeDomainId` to the fetch-effect deps so switching refetches.
 *
 * Convention: skip-guarded against a live Vite dev server (mirrors
 * connect-tenant.spec.ts / embed-integration.spec.ts). Backend responses are
 * route-mocked; no real Django/DB is required. Note the artifacts endpoint
 * returns `{ results: [...] }` (unlike recipes, which return a bare array).
 */

const VITE_URL = "http://localhost:5173";

const WS_A = "11111111-1111-1111-1111-111111111111";
const WS_B = "22222222-2222-2222-2222-222222222222";

function workspace(id: string, displayName: string) {
  return {
    id,
    name: displayName,
    display_name: displayName,
    is_auto_created: false,
    role: "read_write",
    tenants: [
      {
        id: `tenant-${id}`,
        tenant_name: displayName,
        provider: "commcare",
      },
    ],
    member_count: 1,
    schema_status: "available",
    last_synced_at: null,
    created_at: "2026-01-01T00:00:00Z",
  };
}

function artifact(id: string, title: string) {
  return {
    id,
    title,
    description: `Artifact ${title}`,
    artifact_type: "react",
    version: 1,
    has_live_queries: false,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

// Artifacts returned per workspace id, with unique titles so we can assert the
// list actually swapped after a switch.
const ARTIFACTS_BY_WS: Record<string, ReturnType<typeof artifact>[]> = {
  [WS_A]: [artifact("artifact-a1", "Alpha Dashboard")],
  [WS_B]: [artifact("artifact-b1", "Beta Chart")],
};

/** Extract the workspace UUID from a `/api/workspaces/{id}/...` request URL. */
function workspaceIdFromUrl(rawUrl: string): string | null {
  const match = new URL(rawUrl).pathname.match(/\/api\/workspaces\/([^/]+)\//);
  return match ? match[1] : null;
}

async function mockBackend(page: import("@playwright/test").Page) {
  await page.route("**/api/auth/csrf/", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ csrfToken: "fake-csrf" }),
    }),
  );
  await page.route("**/api/auth/me/", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "1",
        email: "test@x.com",
        name: "Test",
        is_staff: false,
        onboarding_complete: true,
      }),
    }),
  );

  // Workspace-scoped endpoints (artifacts list, threads). The `*` matches the
  // workspace id and any trailing query string (e.g. `?search=`).
  await page.route("**/api/workspaces/*/artifacts/*", (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const wsId = workspaceIdFromUrl(route.request().url());
    const artifacts = (wsId && ARTIFACTS_BY_WS[wsId]) || [];
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ results: artifacts }),
    });
  });
  // Same, but for the no-query-string form `/artifacts/`.
  await page.route("**/api/workspaces/*/artifacts/", (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const wsId = workspaceIdFromUrl(route.request().url());
    const artifacts = (wsId && ARTIFACTS_BY_WS[wsId]) || [];
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ results: artifacts }),
    });
  });
  await page.route("**/api/workspaces/*/threads/", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    }),
  );

  await page.route("**/api/workspaces/", (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() === "GET" && url.pathname === "/api/workspaces/") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          workspace(WS_A, "Workspace Alpha"),
          workspace(WS_B, "Workspace Beta"),
        ]),
      });
    }
    return route.continue();
  });
}

async function switchWorkspace(page: import("@playwright/test").Page, wsId: string) {
  await page.locator('[data-testid="domain-selector"]').first().click();
  await page.locator(`[data-testid="domain-item-${wsId}"]`).click();
}

test.describe("Workspace switch — ArtifactsPage refetch (arch #247)", () => {
  test.beforeAll(async () => {
    try {
      const response = await fetch(`${VITE_URL}/`);
      if (!response.ok) throw new Error("Vite not ready");
    } catch {
      test.skip(true, "Vite dev server not running — skipping refetch e2e");
    }
  });

  test.beforeEach(async ({ page }) => {
    await mockBackend(page);
  });

  test("switching workspace shows the new workspace's artifacts and drops the old", async ({
    page,
  }) => {
    await page.goto(`${VITE_URL}/artifacts`);

    // Workspace Alpha is domains[0] → the default active workspace.
    await expect(
      page.locator('[data-testid="artifact-card-artifact-a1"]'),
    ).toBeVisible();
    await expect(page.getByText("Alpha Dashboard")).toBeVisible();
    await expect(
      page.locator('[data-testid="artifact-card-artifact-b1"]'),
    ).toHaveCount(0);

    // Switch to Workspace Beta.
    await switchWorkspace(page, WS_B);

    // Beta's artifact appears...
    await expect(
      page.locator('[data-testid="artifact-card-artifact-b1"]'),
    ).toBeVisible();
    await expect(page.getByText("Beta Chart")).toBeVisible();

    // ...and Alpha's stale artifact is gone (the core regression assertion).
    await expect(
      page.locator('[data-testid="artifact-card-artifact-a1"]'),
    ).toHaveCount(0);
    await expect(page.getByText("Alpha Dashboard")).not.toBeVisible();
  });

  test("switching back to the original workspace restores its artifacts", async ({
    page,
  }) => {
    await page.goto(`${VITE_URL}/artifacts`);
    await expect(
      page.locator('[data-testid="artifact-card-artifact-a1"]'),
    ).toBeVisible();

    await switchWorkspace(page, WS_B);
    await expect(
      page.locator('[data-testid="artifact-card-artifact-b1"]'),
    ).toBeVisible();

    await switchWorkspace(page, WS_A);
    await expect(
      page.locator('[data-testid="artifact-card-artifact-a1"]'),
    ).toBeVisible();
    await expect(
      page.locator('[data-testid="artifact-card-artifact-b1"]'),
    ).toHaveCount(0);
  });
});
