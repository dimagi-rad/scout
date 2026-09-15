import { defineConfig, devices } from "@playwright/test"

// Isolated frontend-only fixture tests: every backend request is intercepted.
export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: "artifact-workspace-links.spec.ts",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  use: {
    baseURL: "http://127.0.0.1:5183",
    channel: process.env.PLAYWRIGHT_CHANNEL,
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile", use: { ...devices["Pixel 7"] } },
  ],
  webServer: {
    command: "bun dev --host 127.0.0.1 --port 5183 --strictPort",
    url: "http://127.0.0.1:5183",
    reuseExistingServer: false,
  },
})
