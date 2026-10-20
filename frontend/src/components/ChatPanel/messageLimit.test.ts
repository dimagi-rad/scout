import { readFileSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"

import { MAX_MESSAGE_LENGTH } from "./messageLimit"

describe("MAX_MESSAGE_LENGTH", () => {
  it("matches the server's limit in apps/chat/constants.py", () => {
    const constants = readFileSync(
      path.resolve(__dirname, "../../../../apps/chat/constants.py"),
      "utf8",
    )
    const match = constants.match(/^MAX_MESSAGE_LENGTH\s*=\s*([\d_]+)/m)
    expect(match).not.toBeNull()
    expect(Number(match![1].replaceAll("_", ""))).toBe(MAX_MESSAGE_LENGTH)
  })
})
