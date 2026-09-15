import type { DomainSlice } from "./domainSlice"

// Each slice owns its read slots; mutations only need workspace ownership.
export function createWorkspaceRequestGuard(get: () => DomainSlice) {
  const requests = new Map<string, symbol>()
  return {
    start: (slot?: string, workspaceId = get().activeDomainId) => {
      const generation = get().workspaceGeneration
      const token = Symbol()
      if (slot) requests.set(slot, token)
      return () =>
        get().activeDomainId === workspaceId &&
        get().workspaceGeneration === generation &&
        (!slot || requests.get(slot) === token)
    },
    invalidate: () => requests.clear(),
  }
}
