import { afterEach } from "vitest"
import { cleanup } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"

// Node 26 exposes an incomplete experimental localStorage global unless it is
// launched with a backing file. Give jsdom tests a browser-compatible store so
// the runtime implementation cannot shadow or disable storage-based behavior.
const storageValues = new Map<string, string>()
const testLocalStorage: Storage = {
  get length() {
    return storageValues.size
  },
  clear() {
    storageValues.clear()
  },
  getItem(key) {
    return storageValues.get(key) ?? null
  },
  key(index) {
    return Array.from(storageValues.keys())[index] ?? null
  },
  removeItem(key) {
    storageValues.delete(key)
  },
  setItem(key, value) {
    storageValues.set(key, String(value))
  },
}

Object.defineProperty(window, "localStorage", {
  configurable: true,
  value: testLocalStorage,
})
Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  value: testLocalStorage,
})

// Unmount React trees between tests even though globals/auto-cleanup is on —
// explicit and resilient to config changes.
afterEach(() => {
  cleanup()
})
