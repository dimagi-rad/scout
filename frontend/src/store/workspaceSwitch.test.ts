import { beforeEach, describe, expect, it } from "vitest"
import { useAppStore } from "@/store/store"

/**
 * Workspace-switch state-reset contract. Coordinates with issue #247.
 *
 * PASSING NOW: switching workspaces starts a fresh per-workspace thread — the
 * only per-workspace state reset that exists today (from the 00c423d
 * threadId-leak fix).
 *
 * SKIPPED (#247): on a workspace switch the pages must also refetch / clear
 * their per-workspace state. #247 supplies the runtime fix; when it lands, flip
 * the `it.skip` calls below to `it` and assert the refetch/clear behaviour.
 */
describe("workspace switch resets per-workspace state", () => {
  beforeEach(() => {
    useAppStore.setState({ activeDomainId: "ws-a", threadId: "thread-a" })
  })

  it("starts a fresh thread for the new workspace (no cross-workspace carry)", () => {
    const before = useAppStore.getState().threadId
    useAppStore.getState().domainActions.setActiveDomain("ws-b")
    expect(useAppStore.getState().threadId).not.toBe(before)
  })

  // --- #247: pages don't refetch / clear state on workspace switch ---
  it.skip("refetches the recipes list on workspace switch (#247, 04#9)", () => {})
  it.skip("refetches the artifacts list on workspace switch (#247, 04#9)", () => {})
  it.skip("clears a prior workspace-detail load error on later success (#247, 05#5)", () => {})
})
