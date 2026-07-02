import { describe, expect, it } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"
import type { UIMessage } from "ai"
import { ChatMessage } from "./ChatMessage"
import { useAppStore } from "@/store/store"

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
  it("renders the rich semantic query card from a live JSON-string output", () => {
    const msg = liveMessage("semantic_query", {
      success: true,
      data: {
        columns: ["id", "name"],
        rows: [
          [1, "Alice"],
          [2, "Bob"],
        ],
        row_count: 2,
        semantic_query: { measures: ["users.count"], dimensions: ["users.name"] },
        members: ["users.name", "users.count"],
      },
    })
    render(<ChatMessage message={msg} isActiveMessage={true} />)
    // Rich card markers (not a raw <pre> dump):
    expect(screen.getByText("Semantic query succeeded")).toBeInTheDocument()
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
    const msg = liveMessage("semantic_query", {
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

  it("groups subagent child tool calls under the parent tool card", () => {
    const msg = {
      id: "m3",
      role: "assistant",
      parts: [
        {
          type: "tool-artifact_manager",
          toolName: "artifact_manager",
          toolCallId: "toolu_PARENT",
          state: "output-available",
          input: { task: "Create a dashboard" },
          output: JSON.stringify({ status: "done", message: "Created dashboard" }),
        },
        {
          type: "data-subagent-tool-input",
          id: "artifact_manager_toolu_CHILD:input",
          data: {
            parentToolCallId: "toolu_PARENT",
            subagentName: "artifact_manager",
            toolCallId: "artifact_manager_toolu_CHILD",
            toolName: "artifact_write",
            input: { action: "create" },
          },
        },
        {
          type: "data-subagent-tool-output",
          id: "artifact_manager_toolu_CHILD:output",
          data: {
            parentToolCallId: "toolu_PARENT",
            subagentName: "artifact_manager",
            toolCallId: "artifact_manager_toolu_CHILD",
            toolName: "artifact_write",
            output: JSON.stringify({ status: "created" }),
          },
        },
        {
          type: "data-subagent-status",
          id: "artifact_manager_status",
          data: {
            parentToolCallId: "toolu_PARENT",
            subagentName: "artifact_manager",
            phase: "running",
            message: "Artifact Manager started.",
          },
        },
        {
          type: "data-subagent-text",
          id: "artifact_manager_text",
          data: {
            parentToolCallId: "toolu_PARENT",
            subagentName: "artifact_manager",
            text: "I inspected the artifact blocks.",
          },
        },
      ],
    } as unknown as UIMessage

    render(<ChatMessage message={msg} isActiveMessage={false} />)

    expect(screen.getByTestId("tool-call-artifact_manager")).toBeInTheDocument()
    expect(screen.getByText("Artifact Manager")).toBeInTheDocument()
    expect(screen.queryByText("subagent")).not.toBeInTheDocument()
    expect(screen.queryByText("1 call")).not.toBeInTheDocument()
    expect(screen.getByTestId("subagent-activity-log")).toBeInTheDocument()
    expect(screen.getByText("I inspected the artifact blocks.")).toBeInTheDocument()
    expect(screen.getByTestId("tool-call-children-artifact_manager")).toBeInTheDocument()
    expect(screen.getByTestId("tool-call-artifact_write")).toBeInTheDocument()
    expect(screen.queryByText(/"status": "created"/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId("tool-call-artifact_write"))
    expect(screen.getByText(/"status": "created"/)).toBeInTheDocument()

    fireEvent.click(screen.getByTestId("tool-call-artifact_manager"))
    expect(screen.queryByTestId("tool-call-artifact_write")).not.toBeInTheDocument()
  })

  it("renders parent tool cards without child events", () => {
    const msg = liveMessage("artifact_manager", {
      status: "done",
      message: "Checked the artifact",
    })
    render(<ChatMessage message={msg} isActiveMessage={false} />)

    expect(screen.getByTestId("tool-call-artifact_manager")).toBeInTheDocument()
    expect(screen.queryByText("nested")).not.toBeInTheDocument()
  })

  it("shows a view artifact button for artifact manager output", () => {
    const artifactId = "22222222-2222-2222-2222-222222222222"
    useAppStore.setState({ activeArtifactId: null })
    const msg = liveMessage("artifact_manager", {
      status: "done",
      artifact_id: artifactId,
      artifact_version: 1,
      message: "Created dashboard",
    })

    render(<ChatMessage message={msg} isActiveMessage={false} />)

    fireEvent.click(screen.getByText("View Artifact"))

    expect(useAppStore.getState().activeArtifactId).toBe(artifactId)
  })

  it("shows loading feedback for an artifact manager subagent with no child events yet", () => {
    const msg = {
      id: "m4",
      role: "assistant",
      parts: [
        {
          type: "tool-artifact_manager",
          toolName: "artifact_manager",
          toolCallId: "toolu_PARENT",
          state: "input-available",
          input: { task: "Create a dashboard" },
        },
      ],
    } as unknown as UIMessage

    render(<ChatMessage message={msg} isActiveMessage={true} />)

    expect(screen.queryByText("subagent")).not.toBeInTheDocument()
    expect(screen.getByText("working")).toBeInTheDocument()
    expect(screen.getByText("Starting Artifact Manager...")).toBeInTheDocument()
  })
})
