import { beforeEach, describe, expect, it, vi } from "vitest"
import {
  clearAllDrafts,
  DRAFT_MAX_AGE_MS,
  DRAFT_MAX_ENTRIES,
  pruneDrafts,
  readDraft,
  writeDraft,
} from "./draftStorage"

function seed(key: string, updatedAt: number) {
  localStorage.setItem(`scout:draft:${key}`, JSON.stringify({ text: key, updatedAt }))
}

describe("draftStorage", () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it("round-trips a draft per workspace and thread under the documented key", () => {
    writeDraft("ws", "t1", "hello")
    writeDraft("ws", "t2", "world")
    expect(readDraft("ws", "t1")).toBe("hello")
    expect(readDraft("ws", "t2")).toBe("world")
    expect(localStorage.getItem("scout:draft:ws:t1")).not.toBeNull()
  })

  it("removes the entry when written empty", () => {
    writeDraft("ws", "t1", "hello")
    writeDraft("ws", "t1", "")
    expect(localStorage.getItem("scout:draft:ws:t1")).toBeNull()
  })

  it("clearAllDrafts removes only draft entries", () => {
    writeDraft("ws", "t1", "a")
    localStorage.setItem("scout:thread:ws", "keep")
    clearAllDrafts()
    expect(localStorage.getItem("scout:draft:ws:t1")).toBeNull()
    expect(localStorage.getItem("scout:thread:ws")).toBe("keep")
  })

  it("never throws when storage is unavailable", () => {
    const boom = () => {
      throw new Error("blocked")
    }
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(boom)
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(boom)
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(boom)
    expect(readDraft("ws", "t1")).toBe("")
    expect(() => writeDraft("ws", "t1", "x")).not.toThrow()
    expect(() => writeDraft("ws", "t1", "")).not.toThrow()
    expect(() => pruneDrafts()).not.toThrow()
    expect(() => clearAllDrafts()).not.toThrow()
    vi.restoreAllMocks()
  })

  it("prunes drafts older than 30 days and malformed entries, keeping fresh ones", () => {
    const now = 10_000_000_000_000
    seed("ws:old", now - DRAFT_MAX_AGE_MS - 1)
    seed("ws:fresh", now - 1000)
    localStorage.setItem("scout:draft:ws:bad", "not json")
    localStorage.setItem("scout:other", "keep")
    pruneDrafts(now)
    expect(localStorage.getItem("scout:draft:ws:old")).toBeNull()
    expect(localStorage.getItem("scout:draft:ws:bad")).toBeNull()
    expect(localStorage.getItem("scout:draft:ws:fresh")).not.toBeNull()
    expect(localStorage.getItem("scout:other")).toBe("keep")
  })

  it("keeps only the newest 50 drafts", () => {
    const now = 10_000_000_000_000
    for (let i = 0; i < DRAFT_MAX_ENTRIES + 5; i++) seed(`ws:t${i}`, now - i * 1000)
    pruneDrafts(now)
    const remaining = Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i)).filter((k) =>
      k?.startsWith("scout:draft:"),
    )
    expect(remaining).toHaveLength(DRAFT_MAX_ENTRIES)
    expect(localStorage.getItem("scout:draft:ws:t0")).not.toBeNull()
    expect(localStorage.getItem(`scout:draft:ws:t${DRAFT_MAX_ENTRIES}`)).toBeNull()
  })
})
