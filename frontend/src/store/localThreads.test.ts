import { afterEach, describe, expect, it, vi } from "vitest"
import { useAppStore } from "./store"
import { isLocalThread } from "./localThreads"

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("locally made-up thread ids", () => {
  it("marks a New chat as local, and forgets it once opened from the thread list", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json([])))
    useAppStore.getState().uiActions.newThread()
    const id = useAppStore.getState().threadId
    expect(isLocalThread(id)).toBe(true)

    // Another tab may have sent in it since, so from the list it has history to load.
    await useAppStore.getState().uiActions.selectThread(id)
    expect(isLocalThread(id)).toBe(false)
  })
})
