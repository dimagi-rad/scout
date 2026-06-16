import { test, expect } from "@playwright/test";

/**
 * Workspace-switch refetch coverage for RecipesPage (arch #247).
 *
 * Regression guard: RecipesPage used to fetch its list only on mount, with an
 * effect dependency array that excluded `activeDomainId`. Switching the active
 * workspace therefore left the PREVIOUS workspace's recipes on screen, and any
 * follow-up action combined the new `activeDomainId` with a stale recipe id and
 * 404'd. The fix adds `activeDomainId` to the fetch-effect deps so switching
 * refetches. These specs assert the NEW workspace's recipes render and the OLD
 * workspace's recipes are gone after a switch.
 *
 * Convention: skip-guarded against a live Vite dev server (mirrors
 * connect-tenant.spec.ts / embed-integration.spec.ts), so the spec is
 * well-formed and CI-safe and executes for real once the stack is up.
 * Backend responses are route-mocked; no real Django/DB is required.
 */

const VITE_URL = "http://localhost:5173";

// Two workspaces the user belongs to. `id` is the workspace UUID that gets
// baked into the `/api/workspaces/{id}/...` request path (the active workspace
// is selected purely client-side; switching never hits the backend).
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

function recipe(id: string, name: string) {
  return {
    id,
    name,
    description: `Recipe ${name}`,
    prompt: "do the thing",
    variables: [],
    is_shared: false,
    variable_count: 0,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

// Recipes returned per workspace id. Names are unique per workspace so we can
// assert the list actually swapped (not just re-rendered the same items).
const RECIPES_BY_WS: Record<string, ReturnType<typeof recipe>[]> = {
  [WS_A]: [recipe("recipe-a1", "Alpha Cohort Report")],
  [WS_B]: [recipe("recipe-b1", "Beta Revenue Rollup")],
};

/** Extract the workspace UUID from a `/api/workspaces/{id}/...` request URL. */
function workspaceIdFromUrl(rawUrl: string): string | null {
  const match = new URL(rawUrl).pathname.match(/\/api\/workspaces\/([^/]+)\//);
  return match ? match[1] : null;
}

async function mockBackend(page: import("@playwright/test").Page) {
  // --- Auth: lets App.tsx reach the authenticated router ---
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

  // --- Workspace-scoped endpoints (recipes list, threads). Ordered BEFORE the
  // bare `/api/workspaces/` list route so the more specific pattern wins. ---
  await page.route("**/api/workspaces/*/recipes/", (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const wsId = workspaceIdFromUrl(route.request().url());
    const recipes = (wsId && RECIPES_BY_WS[wsId]) || [];
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(recipes),
    });
  });
  // Sidebar fetches threads whenever the active workspace changes.
  await page.route("**/api/workspaces/*/threads/", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    }),
  );

  // --- Workspace list: powers the switcher + seeds activeDomainId (domains[0]) ---
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

/** Open the switcher and select the workspace row with the given id. */
async function switchWorkspace(page: import("@playwright/test").Page, wsId: string) {
  await page.locator('[data-testid="domain-selector"]').first().click();
  await page.locator(`[data-testid="domain-item-${wsId}"]`).click();
}

test.describe("Workspace switch — RecipesPage refetch (arch #247)", () => {
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

  test("switching workspace shows the new workspace's recipes and drops the old", async ({
    page,
  }) => {
    await page.goto(`${VITE_URL}/recipes`);

    // Workspace Alpha is domains[0] → the default active workspace.
    await expect(
      page.locator('[data-testid="recipe-card-recipe-a1"]'),
    ).toBeVisible();
    await expect(page.getByText("Alpha Cohort Report")).toBeVisible();
    await expect(
      page.locator('[data-testid="recipe-card-recipe-b1"]'),
    ).toHaveCount(0);

    // Switch to Workspace Beta.
    await switchWorkspace(page, WS_B);

    // Beta's recipe appears...
    await expect(
      page.locator('[data-testid="recipe-card-recipe-b1"]'),
    ).toBeVisible();
    await expect(page.getByText("Beta Revenue Rollup")).toBeVisible();

    // ...and Alpha's stale recipe is gone (the core regression assertion).
    await expect(
      page.locator('[data-testid="recipe-card-recipe-a1"]'),
    ).toHaveCount(0);
    await expect(page.getByText("Alpha Cohort Report")).not.toBeVisible();
  });

  test("switching back to the original workspace restores its recipes", async ({
    page,
  }) => {
    await page.goto(`${VITE_URL}/recipes`);
    await expect(
      page.locator('[data-testid="recipe-card-recipe-a1"]'),
    ).toBeVisible();

    await switchWorkspace(page, WS_B);
    await expect(
      page.locator('[data-testid="recipe-card-recipe-b1"]'),
    ).toBeVisible();

    await switchWorkspace(page, WS_A);
    await expect(
      page.locator('[data-testid="recipe-card-recipe-a1"]'),
    ).toBeVisible();
    await expect(
      page.locator('[data-testid="recipe-card-recipe-b1"]'),
    ).toHaveCount(0);
  });
});
