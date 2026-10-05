import type { UIMessage } from "ai"
import { expect, it } from "vitest"
import { messageArtifacts } from "./artifactReferences"

it.each([null, {}, { output: { artifact_id: "orphan" } }, { parentToolCallId: 1, output: { artifact_id: "orphan" } }])(
  "ignores malformed helper artifact events: %j", (data) => {
    const message = { id: "bad-event", role: "assistant", parts: [
      { type: "data-subagent-tool-output", data },
    ] } as unknown as UIMessage
    expect(messageArtifacts(message).size).toBe(0)
  },
)
