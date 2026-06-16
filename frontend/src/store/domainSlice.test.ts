import { beforeEach, describe, expect, it } from "vitest"
import { useAppStore } from "@/store/store"

describe("domainSlice.setActiveDomain — threadId leak guard (00c423d)", () => {
  beforeEach(() => {
    useAppStore.setState({ activeDomainId: "ws-a", threadId: "thread-a" })
  })

  it("resets threadId to a fresh id when switching to a different workspace", () => {
    useAppStore.getState().domainActions.setActiveDomain("ws-b")
    const s = useAppStore.getState()
    expect(s.activeDomainId).toBe("ws-b")
    expect(s.threadId).not.toBe("thread-a")
    // fresh client-generated UUID
    expect(s.threadId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
    )
  })

  it("keeps threadId when re-selecting the same workspace", () => {
    useAppStore.getState().domainActions.setActiveDomain("ws-a")
    expect(useAppStore.getState().activeDomainId).toBe("ws-a")
    expect(useAppStore.getState().threadId).toBe("thread-a")
  })
})
