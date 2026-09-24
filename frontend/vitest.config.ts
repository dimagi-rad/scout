import { defineConfig } from "vitest/config"
import react from "@vitejs/plugin-react"
import path from "path"

// Components format with the runtime's default locale, as they should for users,
// and the tests assert concrete en-US strings ("$125.00", "Jan 5"). Pin the locale
// the worker processes inherit so the suite passes the same on an en-GB laptop as
// in CI. ICU reads it once at process start, so this only reaches the tests
// because the forks pool spawns a fresh process per worker.
process.env.LC_ALL = "en_US.UTF-8"

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    pool: "forks",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
})
