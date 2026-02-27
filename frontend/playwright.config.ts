import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: "html",
  use: {
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "widget-sdk",
      testMatch: "widget-sdk.spec.ts",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "embed-integration",
      testMatch: "embed-integration.spec.ts",
      use: { ...devices["Desktop Chrome"] },
      ...(process.env.SKIP_WEBSERVER
        ? {}
        : {
            webServer: [
              {
                command: "cd .. && uv run uvicorn config.asgi:application --port 8000",
                port: 8000,
                reuseExistingServer: true,
                timeout: 30_000,
              },
              {
                command: "bun dev",
                port: 5173,
                reuseExistingServer: true,
                timeout: 15_000,
              },
            ],
          }),
    },
  ],
});
