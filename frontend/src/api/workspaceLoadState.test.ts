import { describe, expect, it } from "vitest"
import { workspaceHasRecordedLoad, workspaceLoadState } from "./workspaces"

const last_synced_at = "2026-04-16T12:00:00Z"

describe("workspace recorded load metadata", () => {
  it.each(["available", "provisioning", "unavailable", "failed"] as const)(
    "only treats an actual load timestamp as recorded history for %s",
    (schema_status) => {
      const metadata = { schema_status, last_synced_at }
      expect(workspaceHasRecordedLoad(metadata)).toBe(true)
      expect(workspaceHasRecordedLoad({ ...metadata, last_synced_at: null })).toBe(false)
    },
  )

  it.each([
    [{ last_synced_at }, "recorded", true],
    [{ last_synced_at: null }, "unknown", false],
    [{ last_synced_at: "" }, "unknown", false],
    [{}, "unknown", false],
  ] as const)("makes no readiness claim for legacy cached payload %j", (metadata, state, recorded) => {
    expect(workspaceLoadState(metadata)).toBe(state)
    expect(workspaceHasRecordedLoad(metadata)).toBe(recorded)
  })
})
