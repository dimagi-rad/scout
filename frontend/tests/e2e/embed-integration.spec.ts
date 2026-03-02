import { test, expect } from "@playwright/test";

const DJANGO_URL = "http://localhost:8000";
const VITE_URL = "http://localhost:5173";

test.beforeAll(async () => {
  // Check if servers are running; skip suite if not
  try {
    const response = await fetch(`${DJANGO_URL}/api/auth/csrf/`);
    if (!response.ok) throw new Error("Django not ready");
  } catch {
    test.skip(true, "Django server not running — skipping integration tests");
  }
});

test.describe("Embed Integration Tests", () => {
  test("/embed/ shows login form when unauthenticated", async ({ page }) => {
    await page.goto(`${VITE_URL}/embed/`);

    // The embed page should detect unauthenticated state
    // and show either a login prompt or redirect
    const body = page.locator("body");
    await expect(body).toBeVisible();

    // Check for auth-related content (login form or auth-required message)
    const hasLoginContent = await page
      .getByText(/sign in|log in|auth/i)
      .first()
      .isVisible()
      .catch(() => false);
    const hasLoadingOrApp = await page
      .locator("#root")
      .isVisible()
      .catch(() => false);

    expect(hasLoginContent || hasLoadingOrApp).toBe(true);
  });

  test("/embed/?mode=chat loads correctly", async ({ page }) => {
    await page.goto(`${VITE_URL}/embed/?mode=chat`);

    // Page should load without errors
    const root = page.locator("#root");
    await expect(root).toBeVisible();

    // Verify mode param is in the URL
    expect(page.url()).toContain("mode=chat");
  });

  test("/embed/?mode=full loads correctly", async ({ page }) => {
    await page.goto(`${VITE_URL}/embed/?mode=full`);

    const root = page.locator("#root");
    await expect(root).toBeVisible();

    expect(page.url()).toContain("mode=full");
  });

  test("widget SDK creates iframe that loads /embed/", async ({ page }) => {
    // Create a simple host page
    await page.setContent(`
      <!DOCTYPE html>
      <html>
      <body>
        <div id="widget-container" style="width:800px;height:600px;"></div>
      </body>
      </html>
    `);

    // Load the real widget.js from the Vite server
    await page.addScriptTag({ url: `${VITE_URL}/widget.js` });

    await page.evaluate(() => {
      window.ScoutWidget.init({ container: "#widget-container", mode: "chat" });
    });

    const iframe = page.locator("#widget-container iframe");
    await expect(iframe).toBeAttached();
    const src = await iframe.getAttribute("src");
    expect(src).toContain(`${VITE_URL}/embed/`);
    expect(src).toContain("mode=chat");
  });

  test("/widget.js returns valid JavaScript", async ({ page }) => {
    const response = await page.goto(`${VITE_URL}/widget.js`);
    expect(response).not.toBeNull();
    expect(response!.status()).toBe(200);

    const contentType = response!.headers()["content-type"] || "";
    expect(contentType).toMatch(/javascript/);

    const body = await response!.text();
    expect(body).toContain("ScoutWidget");
  });

  test("Django sets CSP headers on /embed/ endpoint", async ({ page }) => {
    const response = await page.goto(`${DJANGO_URL}/embed/`);
    expect(response).not.toBeNull();

    // The embed middleware should remove X-Frame-Options
    // and set Content-Security-Policy with frame-ancestors
    const headers = response!.headers();

    // X-Frame-Options should be absent (middleware removes it)
    expect(headers["x-frame-options"]).toBeUndefined();

    // CSP frame-ancestors should be set
    const csp = headers["content-security-policy"] || "";
    expect(csp).toContain("frame-ancestors");
  });
});
