import { expect, it } from "vitest"
import { internalChatPath } from "./chatLinks"

const ORIGIN = "https://scout.example"

it.each(["/\\evil.example", "//evil.example", "/\t/evil.example", "https://evil.example/chart"])(
  "keeps cross-origin authority %j out of the client router", (href) => {
    expect(internalChatPath(href, ORIGIN)).toBeNull()
  },
)

it("preserves the path, query, and fragment of an in-app link", () => {
  expect(internalChatPath("/workspaces/w/artifacts/a?view=chart#table", ORIGIN))
    .toBe("/workspaces/w/artifacts/a?view=chart#table")
  expect(internalChatPath("#footnote", ORIGIN)).toBeNull()
})
