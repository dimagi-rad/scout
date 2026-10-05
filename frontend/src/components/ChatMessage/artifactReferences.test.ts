import type { UIMessage } from "ai"
import { afterEach, expect, it, vi } from "vitest"
import { isUnsuccessfulHelperOutcome, messageArtifacts, turnArtifactOwners } from "./artifactReferences"

afterEach(() => vi.restoreAllMocks())

it.each([null, {}, { output: { artifact_id: "orphan" } }, { parentToolCallId: 1, output: { artifact_id: "orphan" } }])(
  "ignores malformed helper artifact events: %j", (data) => {
    const message = { id: "bad-event", role: "assistant", parts: [
      { type: "data-subagent-tool-output", data },
    ] } as unknown as UIMessage
    expect(messageArtifacts(message).size).toBe(0)
  },
)

it("does not reparse unchanged history during streaming updates", () => {
  const history = { id: "history", role: "assistant", parts: [
    { type: "tool-artifact_manager", toolCallId: "history-call", state: "output-available", input: {},
      output: JSON.stringify({ artifact_id: "a", artifact_version: 1 }) },
  ] } as UIMessage
  const parse = vi.spyOn(JSON, "parse")
  for (const text of ["Chart", "Chart is", "Chart is ready."]) {
    const streaming = { id: "streaming", role: "assistant", parts: [{ type: "text", text }] } as UIMessage
    expect(turnArtifactOwners([history, streaming]).get(history.id)?.has("a")).toBe(true)
  }
  expect(parse).toHaveBeenCalledTimes(1)
  const updated = { ...history, parts: [{ ...history.parts[0],
    output: JSON.stringify({ artifact_id: "a", artifact_version: 2 }),
  }] } as UIMessage
  expect(messageArtifacts(updated).get("a")?.version).toBe(2)
  expect(parse).toHaveBeenCalledTimes(2)
})

it.each(["failed", "partial", "needs_review", "unknown"])("keeps unknown helper status %s open", (status) => {
  expect(isUnsuccessfulHelperOutcome({ status })).toBe(true)
})
it.each(["done", "ok", "success", "completed", "created", "updated", "replaced", "checked", "OK", "Done", " success "])("accepts successful helper status %s", (status) => {
  expect(isUnsuccessfulHelperOutcome({ status })).toBe(false)
})
