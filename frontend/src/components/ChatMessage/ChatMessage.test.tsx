import { describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"
import type { UIMessage } from "ai"
import { ChatMessage } from "./ChatMessage"

// A live tool part as produced by the SSE stream: `output` is a JSON STRING
// (apps/chat/stream.py emits the MCP envelope as compact JSON). The rich card
// must render LIVE from this string — not fall back to a raw <pre>.
function liveMessage(toolName: string, output: unknown): UIMessage {
  return {
    id: "m1",
    role: "assistant",
    parts: [
      {
        type: `tool-${toolName}`,
        toolName,
        toolCallId: "toolu_LIVE",
        state: "output-available",
        input: {},
        output: typeof output === "string" ? output : JSON.stringify(output),
      },
    ],
  } as unknown as UIMessage
}

describe("ChatMessage live tool cards (arch #246)", () => {
  it("renders the rich query card from a live JSON-string output", () => {
    const msg = liveMessage("query", {
      success: true,
      data: {
        columns: ["id", "name"],
        rows: [
          [1, "Alice"],
          [2, "Bob"],
        ],
        row_count: 2,
        sql_executed: "SELECT id, name FROM users",
      },
    })
    render(<ChatMessage message={msg} isActiveMessage={true} />)
    // Rich card markers (not a raw <pre> dump):
    expect(screen.getByText("Query succeeded")).toBeInTheDocument()
    expect(screen.getByText("2 rows")).toBeInTheDocument()
    expect(screen.getByText("Alice")).toBeInTheDocument()
  })

  it("renders the get_metadata card with the correct table count live", () => {
    const msg = liveMessage("get_metadata", {
      success: true,
      data: { schema: "public", table_count: 4, tables: { a: {}, b: {}, c: {}, d: {} } },
    })
    render(<ChatMessage message={msg} isActiveMessage={true} />)
    expect(screen.getByText("4 tables")).toBeInTheDocument()
    expect(screen.queryByText("0 tables")).not.toBeInTheDocument()
  })

  it("does not corrupt apostrophes in the data (05#2: dropped the repr hack)", () => {
    const msg = liveMessage("query", {
      success: true,
      data: { columns: ["note"], rows: [["it's fine"]], row_count: 1 },
    })
    render(<ChatMessage message={msg} isActiveMessage={true} />)
    expect(screen.getByText("it's fine")).toBeInTheDocument()
  })

  it("renders a reasoning (Thinking) part on reload", () => {
    const msg = {
      id: "m2",
      role: "assistant",
      parts: [
        { type: "reasoning", text: "thinking about the join keys" },
        { type: "text", text: "Here is the answer." },
      ],
    } as unknown as UIMessage
    render(<ChatMessage message={msg} isActiveMessage={false} />)
    expect(screen.getByTestId("thinking-toggle")).toBeInTheDocument()
  })
})
