import { act, renderHook } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import {
  parseFacetSelection,
  readFacetSelection,
  usePersistentFacetSelection,
  writeFacetSelection,
} from "./facetStorage"

const KEYS = ["provider", "status"] as const

beforeEach(() => localStorage.clear())

describe("parseFacetSelection", () => {
  it("keeps known keys and string values", () => {
    expect(
      parseFacetSelection(
        JSON.stringify({ provider: ["ocs", "ocs", 3, ""], status: ["active"], bogus: ["x"] }),
        KEYS,
      ),
    ).toEqual({ provider: ["ocs"], status: ["active"] })
  })

  it.each([
    ["not json", "{nope"],
    ["an array", "[1,2]"],
    ["null", "null"],
    ["a string", '"active"'],
    ["non-array values", '{"provider":"ocs"}'],
  ])("falls back to no filters for %s", (_label, raw) => {
    expect(parseFacetSelection(raw, KEYS)).toEqual({})
  })

  it("ignores inherited keys", () => {
    expect(parseFacetSelection('{"__proto__":{"provider":["ocs"]}}', ["__proto__", ...KEYS])).toEqual({})
  })
})

describe("storage round trip", () => {
  it("writes non-empty facets and removes the entry when nothing is selected", () => {
    writeFacetSelection("k", { provider: ["ocs"], status: [] })
    expect(localStorage.getItem("k")).toBe('{"provider":["ocs"]}')
    expect(readFacetSelection("k", KEYS)).toEqual({ provider: ["ocs"] })
    writeFacetSelection("k", { status: [] })
    expect(localStorage.getItem("k")).toBeNull()
  })
})

describe("usePersistentFacetSelection", () => {
  it("restores and persists the selection", () => {
    localStorage.setItem("scout:test:u1", JSON.stringify({ status: ["inactive"] }))
    const { result } = renderHook(() => usePersistentFacetSelection("scout:test:u1", KEYS))
    expect(result.current[0]).toEqual({ status: ["inactive"] })

    act(() => result.current[1]({ status: ["active"], provider: ["ocs"] }))

    expect(result.current[0]).toEqual({ status: ["active"], provider: ["ocs"] })
    expect(JSON.parse(localStorage.getItem("scout:test:u1")!)).toEqual({
      status: ["active"],
      provider: ["ocs"],
    })
  })

  it("starts empty when storage is corrupt", () => {
    localStorage.setItem("scout:test:u1", "{broken")
    const { result } = renderHook(() => usePersistentFacetSelection("scout:test:u1", KEYS))
    expect(result.current[0]).toEqual({})
  })

  it("follows the user's choice when storage rejects the write", () => {
    const { result } = renderHook(() => usePersistentFacetSelection("scout:test:u1", KEYS))
    const setItem = vi.spyOn(localStorage, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError")
    })
    act(() => result.current[1]({ status: ["active"] }))
    setItem.mockRestore()
    expect(result.current[0]).toEqual({ status: ["active"] })
  })

  it("does not resurface an in-memory selection after the key changes", () => {
    const { result, rerender } = renderHook(
      ({ storageKey }) => usePersistentFacetSelection(storageKey, KEYS),
      { initialProps: { storageKey: null as string | null } },
    )
    act(() => result.current[1]({ status: ["active"] }))
    rerender({ storageKey: "scout:test:u1" })
    rerender({ storageKey: null })
    expect(result.current[0]).toEqual({})
  })

  it("re-reads when the key changes, so another user's filters never carry over", () => {
    localStorage.setItem("scout:test:u1", JSON.stringify({ status: ["inactive"] }))
    const { result, rerender } = renderHook(
      ({ storageKey }) => usePersistentFacetSelection(storageKey, KEYS),
      { initialProps: { storageKey: "scout:test:u1" as string | null } },
    )
    rerender({ storageKey: "scout:test:u2" })
    expect(result.current[0]).toEqual({})
    rerender({ storageKey: null })
    act(() => result.current[1]({ status: ["active"] }))
    expect(result.current[0]).toEqual({ status: ["active"] })
    expect(localStorage.length).toBe(1)
  })
})
