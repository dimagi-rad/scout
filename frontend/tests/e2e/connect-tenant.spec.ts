import { test, expect } from "@playwright/test";
import { readFileSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// ---------------------------------------------------------------------------
// Describe Block 1: Widget SDK Tests (no servers needed, pure route mocking)
// ---------------------------------------------------------------------------

const WIDGET_JS = readFileSync(
  resolve(__dirname, "../../public/widget.js"),
  "utf-8"
);

const FIXTURE_HTML = readFileSync(
  resolve(__dirname, "fixtures/widget-test.html"),
  "utf-8"
);

const HOST_ORIGIN = "http://localhost:4000";
const SCOUT_ORIGIN = "http://localhost:5173";

/**
 * Set up route interception so the page loads our fixture HTML and widget.js
 * without needing a real server.
 */
async function setupRoutes(page: import("@playwright/test").Page) {
  await page.route(`${HOST_ORIGIN}/**`, async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/" || url.pathname === "/index.html") {
      await route.fulfill({
        status: 200,
        contentType: "text/html",
        body: FIXTURE_HTML,
      });
    } else {
      await route.fulfill({ status: 404, body: "Not found" });
    }
  });

  // Serve widget.js from the Scout origin
  await page.route(`${SCOUT_ORIGIN}/widget.js`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/javascript",
      body: WIDGET_JS,
    });
  });

  // Serve a minimal /embed/ page from the Scout origin
  await page.route(`${SCOUT_ORIGIN}/embed/**`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/html",
      body: `<!DOCTYPE html><html><body><div id="embed-root">Scout Embed</div></body></html>`,
    });
  });
  await page.route(`${SCOUT_ORIGIN}/embed/`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/html",
      body: `<!DOCTYPE html><html><body><div id="embed-root">Scout Embed</div></body></html>`,
    });
  });
}

/**
 * Load the host page and inject the widget script from the Scout origin.
 */
async function loadPageWithWidget(page: import("@playwright/test").Page) {
  await setupRoutes(page);
  await page.goto(HOST_ORIGIN);
  await page.addScriptTag({ url: `${SCOUT_ORIGIN}/widget.js` });
}

test.describe("Widget SDK – Connect provider param", () => {
  test("provider param passed as query param", async ({ page }) => {
    await loadPageWithWidget(page);

    await page.evaluate(() => {
      window.ScoutWidget.init({
        container: "#widget-container",
        tenant: "532",
        provider: "commcare_connect",
      });
    });

    const iframe = page.locator("#widget-container iframe");
    await expect(iframe).toBeAttached();
    const src = await iframe.getAttribute("src");
    expect(src).toContain("tenant=532");
    expect(src).toContain("provider=commcare_connect");
  });

  test("provider defaults to commcare_connect when only tenant set", async ({
    page,
  }) => {
    await loadPageWithWidget(page);

    await page.evaluate(() => {
      window.ScoutWidget.init({
        container: "#widget-container",
        tenant: "532",
      });
    });

    const iframe = page.locator("#widget-container iframe");
    await expect(iframe).toBeAttached();
    const src = await iframe.getAttribute("src");
    expect(src).toContain("tenant=532");
    // widget.js only adds provider= if opts.provider is explicitly set
    expect(src).not.toContain("provider=");
  });
});

// ---------------------------------------------------------------------------
// Describe Block 2: Embed Integration Tests (Vite dev server + API route mocking)
// ---------------------------------------------------------------------------

const VITE_URL = "http://localhost:5173";

test.describe("Embed Integration – Connect tenant", () => {
  test.beforeAll(async () => {
    try {
      const response = await fetch(`${VITE_URL}/`);
      if (!response.ok) throw new Error("Vite not ready");
    } catch {
      test.skip(true, "Vite dev server not running");
    }
  });

  test.beforeEach(async ({ page }) => {
    // Common API mocks for all embed integration tests
    await page.route("**/api/auth/csrf/", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ csrfToken: "fake-csrf" }),
      });
    });

    await page.route("**/api/auth/me/", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "1",
          email: "test@x.com",
          name: "Test",
          is_staff: false,
          onboarding_complete: true,
        }),
      });
    });

    await page.route("**/api/auth/tenants/select/", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "ok" }),
      });
    });

    await page.route("**/api/chat/threads/*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });
  });

  test("embed page calls /api/auth/tenants/ensure/ on load with tenant param", async ({
    page,
  }) => {
    // Mock tenants list (mixed providers)
    await page.route("**/api/auth/tenants/", async (route) => {
      // Only intercept GET requests for the list endpoint (not sub-paths)
      const url = new URL(route.request().url());
      if (
        route.request().method() === "GET" &&
        url.pathname === "/api/auth/tenants/"
      ) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              id: "uuid-1",
              provider: "commcare",
              tenant_id: "dimagi",
              tenant_name: "Dimagi",
              last_selected_at: null,
            },
            {
              id: "uuid-2",
              provider: "commcare_connect",
              tenant_id: "532",
              tenant_name: "Opp 532",
              last_selected_at: null,
            },
          ]),
        });
      } else {
        await route.continue();
      }
    });

    // Capture the POST body sent to /api/auth/tenants/ensure/
    let ensureBody: Record<string, unknown> | null = null;
    await page.route("**/api/auth/tenants/ensure/", async (route) => {
      const request = route.request();
      ensureBody = JSON.parse(request.postData() || "{}");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "uuid-2",
          provider: "commcare_connect",
          tenant_id: "532",
          tenant_name: "Opp 532",
          created: false,
        }),
      });
    });

    await page.goto(`${VITE_URL}/embed/?tenant=532`);

    // Wait for the ensure endpoint to be called
    await expect
      .poll(() => ensureBody, { timeout: 10_000 })
      .not.toBeNull();

    expect(ensureBody).toEqual({
      provider: "commcare_connect",
      tenant_id: "532",
    });
  });

  test("sidebar groups tenants by provider when both exist", async ({
    page,
  }) => {
    // Mock tenants list with both providers
    await page.route("**/api/auth/tenants/", async (route) => {
      const url = new URL(route.request().url());
      if (
        route.request().method() === "GET" &&
        url.pathname === "/api/auth/tenants/"
      ) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              id: "uuid-1",
              provider: "commcare",
              tenant_id: "dimagi",
              tenant_name: "Dimagi",
              last_selected_at: null,
            },
            {
              id: "uuid-2",
              provider: "commcare_connect",
              tenant_id: "532",
              tenant_name: "Opp 532",
              last_selected_at: null,
            },
          ]),
        });
      } else {
        await route.continue();
      }
    });

    await page.goto(`${VITE_URL}/`);

    // Click the domain selector trigger
    await page.locator('[data-testid="domain-selector"]').click();

    // Verify group headers are visible
    await expect(page.getByText("CommCare Domains")).toBeVisible();
    await expect(page.getByText("Connect Opportunities")).toBeVisible();
  });

  test("sidebar shows flat list when single provider", async ({ page }) => {
    // Mock tenants list with only CommCare tenants
    await page.route("**/api/auth/tenants/", async (route) => {
      const url = new URL(route.request().url());
      if (
        route.request().method() === "GET" &&
        url.pathname === "/api/auth/tenants/"
      ) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              id: "uuid-1",
              provider: "commcare",
              tenant_id: "dimagi",
              tenant_name: "Dimagi",
              last_selected_at: null,
            },
            {
              id: "uuid-3",
              provider: "commcare",
              tenant_id: "icds",
              tenant_name: "ICDS",
              last_selected_at: null,
            },
          ]),
        });
      } else {
        await route.continue();
      }
    });

    await page.goto(`${VITE_URL}/`);

    // Click the domain selector trigger
    await page.locator('[data-testid="domain-selector"]').click();

    // Verify the group headers are NOT visible (flat list, no headers)
    await expect(page.getByText("CommCare Domains")).not.toBeVisible();
    await expect(page.getByText("Connect Opportunities")).not.toBeVisible();

    // But the individual items should still be visible in the dropdown
    await expect(page.locator('[data-testid="domain-item-dimagi"]')).toBeVisible();
    await expect(page.locator('[data-testid="domain-item-icds"]')).toBeVisible();
  });
});
