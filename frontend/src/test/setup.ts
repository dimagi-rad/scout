import { afterEach } from "vitest"
import { cleanup } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"

// Unmount React trees between tests even though globals/auto-cleanup is on —
// explicit and resilient to config changes.
afterEach(() => {
  cleanup()
})
