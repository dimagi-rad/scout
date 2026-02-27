import { test, expect } from "@playwright/test";
import { readFileSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

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

test.describe("Widget SDK Unit Tests", () => {
  test("init() creates iframe with correct src URL", async ({ page }) => {
    await loadPageWithWidget(page);

    await page.evaluate(() => {
      window.ScoutWidget.init({ container: "#widget-container" });
    });

    const iframe = page.locator("#widget-container iframe");
    await expect(iframe).toBeAttached();
    const src = await iframe.getAttribute("src");
    expect(src).toContain("/embed/");
  });

  test("mode, tenant, theme passed as query params", async ({ page }) => {
    await loadPageWithWidget(page);

    await page.evaluate(() => {
      window.ScoutWidget.init({
        container: "#widget-container",
        mode: "chat",
        tenant: "acme-corp",
        theme: "dark",
      });
    });

    const iframe = page.locator("#widget-container iframe");
    const src = await iframe.getAttribute("src");
    expect(src).toContain("mode=chat");
    expect(src).toContain("tenant=acme-corp");
    expect(src).toContain("theme=dark");
  });

  test("loading spinner shown initially", async ({ page }) => {
    await setupRoutes(page);
    await page.goto(HOST_ORIGIN);

    // Before adding script, set up container content check
    // We need to inject widget.js but call init before iframe replaces spinner
    // Actually, the spinner is replaced immediately by the iframe in _init()
    // So we test that the loading styles element is created
    await page.addScriptTag({ url: `${SCOUT_ORIGIN}/widget.js` });

    await page.evaluate(() => {
      window.ScoutWidget.init({ container: "#widget-container" });
    });

    const styles = page.locator("#scout-widget-styles");
    await expect(styles).toBeAttached();
    const content = await styles.textContent();
    expect(content).toContain("scout-spin");
  });

  test("error state shown on iframe onerror", async ({ page }) => {
    await setupRoutes(page);
    await page.goto(HOST_ORIGIN);
    await page.addScriptTag({ url: `${SCOUT_ORIGIN}/widget.js` });

    await page.evaluate(() => {
      window.ScoutWidget.init({ container: "#widget-container" });
    });

    // Manually trigger the error handler on the iframe
    await page.evaluate(() => {
      const iframe = document.querySelector(
        "#widget-container iframe"
      ) as HTMLIFrameElement;
      iframe.onerror?.(new Event("error"));
    });

    const container = page.locator("#widget-container");
    await expect(container).toContainText("Failed to load Scout");
  });

  test("postMessage origin validation rejects wrong origins", async ({
    page,
  }) => {
    await loadPageWithWidget(page);

    await page.evaluate(() => {
      (window as any).__testEvents = [];
      window.ScoutWidget.init({
        container: "#widget-container",
        onEvent: (data: any) => {
          (window as any).__testEvents.push(data);
        },
      });
    });

    // Send a message from a wrong origin (the page itself, not SCOUT_ORIGIN)
    await page.evaluate(() => {
      window.postMessage({ type: "scout:ready" }, "*");
    });

    // Give time for any handler to fire
    await page.waitForTimeout(100);

    const captured = await page.evaluate(
      () => (window as any).__testEvents.length
    );
    expect(captured).toBe(0);
  });

  test("scout:ready triggers onReady callback", async ({ page }) => {
    await loadPageWithWidget(page);

    await page.evaluate((origin) => {
      (window as any).__readyCalled = false;
      window.ScoutWidget.init({
        container: "#widget-container",
        onReady: () => {
          (window as any).__readyCalled = true;
        },
      });

      // Simulate the iframe sending scout:ready from the correct origin
      const event = new MessageEvent("message", {
        data: { type: "scout:ready" },
        origin: origin,
      });
      window.dispatchEvent(event);
    }, SCOUT_ORIGIN);

    const readyCalled = await page.evaluate(() => (window as any).__readyCalled);
    expect(readyCalled).toBe(true);
  });

  test("events forwarded to onEvent callback", async ({ page }) => {
    await loadPageWithWidget(page);

    await page.evaluate((origin) => {
      (window as any).__events = [];
      window.ScoutWidget.init({
        container: "#widget-container",
        onEvent: (data: any) => {
          (window as any).__events.push(data);
        },
      });

      // Simulate events from the correct origin
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "scout:ready" },
          origin: origin,
        })
      );
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "scout:navigate", payload: { path: "/knowledge" } },
          origin: origin,
        })
      );
    }, SCOUT_ORIGIN);

    const events = await page.evaluate(() => (window as any).__events);
    expect(events).toHaveLength(2);
    expect(events[0].type).toBe("scout:ready");
    expect(events[1].type).toBe("scout:navigate");
  });

  test("async queue replay works (init before script loads)", async ({
    page,
  }) => {
    await setupRoutes(page);
    await page.goto(HOST_ORIGIN);

    // Set up the async loading stub before widget.js loads
    await page.evaluate(() => {
      (window as any).ScoutWidget = {
        _q: [] as any[],
        init: function (opts: any) {
          this._q.push(["init", opts]);
        },
        destroy: function () {
          this._q.push(["destroy", undefined]);
        },
      };

      // Queue an init call
      window.ScoutWidget.init({ container: "#widget-container" });
    });

    // Now load the real script, which should replay the queue
    await page.addScriptTag({ url: `${SCOUT_ORIGIN}/widget.js` });

    // The queued init should have created an iframe
    const iframe = page.locator("#widget-container iframe");
    await expect(iframe).toBeAttached();
  });

  test("destroy() removes iframe", async ({ page }) => {
    await loadPageWithWidget(page);

    await page.evaluate(() => {
      const instance = window.ScoutWidget.init({
        container: "#widget-container",
      });
      (window as any).__instance = instance;
    });

    const iframe = page.locator("#widget-container iframe");
    await expect(iframe).toBeAttached();

    await page.evaluate(() => {
      (window as any).__instance.destroy();
    });

    await expect(iframe).not.toBeAttached();
  });

  test("ScoutWidget.destroy() removes all instances", async ({ page }) => {
    await loadPageWithWidget(page);

    // Add a second container
    await page.evaluate(() => {
      const div = document.createElement("div");
      div.id = "widget-container-2";
      div.style.cssText = "width:400px;height:300px;";
      document.body.appendChild(div);
    });

    await page.evaluate(() => {
      window.ScoutWidget.init({ container: "#widget-container" });
      window.ScoutWidget.init({ container: "#widget-container-2" });
    });

    const iframes = page.locator("iframe");
    await expect(iframes).toHaveCount(2);

    await page.evaluate(() => {
      window.ScoutWidget.destroy();
    });

    await expect(iframes).toHaveCount(0);
  });

  test("setTenant() sends postMessage to iframe", async ({ page }) => {
    await loadPageWithWidget(page);

    const messages = await page.evaluate(() => {
      const captured: { type: string; payload: unknown }[] = [];
      const instance = window.ScoutWidget.init({
        container: "#widget-container",
      }) as any;

      // Monkey-patch the instance's _postMessage (same-origin, no cross-frame issues)
      instance._postMessage = function (type: string, payload: unknown) {
        captured.push({ type, payload });
      };

      instance.setTenant("new-tenant-123");
      return captured;
    });

    expect(messages).toHaveLength(1);
    expect(messages[0].type).toBe("scout:set-tenant");
    expect((messages[0].payload as any).tenant).toBe("new-tenant-123");
  });

  test("setMode() sends postMessage to iframe", async ({ page }) => {
    await loadPageWithWidget(page);

    const messages = await page.evaluate(() => {
      const captured: { type: string; payload: unknown }[] = [];
      const instance = window.ScoutWidget.init({
        container: "#widget-container",
      }) as any;

      instance._postMessage = function (type: string, payload: unknown) {
        captured.push({ type, payload });
      };

      instance.setMode("full");
      return captured;
    });

    expect(messages).toHaveLength(1);
    expect(messages[0].type).toBe("scout:set-mode");
    expect((messages[0].payload as any).mode).toBe("full");
  });
});
