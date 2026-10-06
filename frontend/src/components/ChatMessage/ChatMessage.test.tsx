import { describe, expect, it } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import type { UIMessage } from "ai"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { turnArtifactOwners } from "./artifactReferences"
import { ChatMessage } from "./ChatMessage"
import { useAppStore } from "@/store/store"

// A live tool part as produced by the SSE stream: `output` is a JSON STRING
// (apps/chat/stream.py emits the MCP envelope as compact JSON). The rich card
// must render LIVE from this string — not fall back to a raw <pre>.
function liveMessage(toolName: string, output: unknown, input: unknown = {}): UIMessage {
  return {
    id: "m1",
    role: "assistant",
    parts: [
      {
        type: `tool-${toolName}`,
        toolName,
        toolCallId: "toolu_LIVE",
        state: "output-available",
        input,
        output: typeof output === "string" ? output : JSON.stringify(output),
      },
    ],
  } as unknown as UIMessage
}

describe("ChatMessage live tool cards (arch #246)", () => {
  it("renders the rich query card with the executed SQL", async () => {
    const msg = liveMessage("query", {
      success: true,
      data: {
        columns: ["id", "name"],
        rows: [[1, "Alice"]],
        row_count: 1,
        sql_executed: "SELECT id, name FROM users LIMIT 500",
        tables_accessed: ["users"],
      },
    })
    render(<ChatMessage message={msg} isActiveMessage={true} />)
    expect(screen.getByText("Query succeeded")).toBeInTheDocument()
    expect(screen.getByText("Alice")).toBeInTheDocument()

    await userEvent.click(screen.getByTestId("query-tab-sql"))
    expect(screen.getByTestId("query-sql")).toHaveTextContent(/FROM\s+users/)
  })

  it("shows the attempted SQL from the tool input when the query fails", () => {
    const msg = liveMessage(
      "query",
      { success: false, error: { code: "QUERY_TIMEOUT", message: "Query timed out." } },
      { sql: "SELECT count(*) FROM big_table" },
    )
    render(<ChatMessage message={msg} isActiveMessage={true} />)
    expect(screen.getByText("Query timed out.")).toBeInTheDocument()
    expect(screen.getByTestId("query-sql")).toHaveTextContent(/FROM\s+big_table/)
  })

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
    expect(screen.getByText("Artifact editor")).toBeInTheDocument()
    expect(screen.queryByText("subagent")).not.toBeInTheDocument()
    expect(screen.queryByText("1 call")).not.toBeInTheDocument()
    expect(screen.queryByTestId("subagent-activity-log")).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId("tool-call-artifact_manager"))
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

  it("renders subagent child tool calls in emitted order with activity text", () => {
    const msg = {
      id: "m3-ordered",
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
          type: "data-subagent-text",
          id: "artifact_manager_text_before",
          data: {
            parentToolCallId: "toolu_PARENT",
            subagentName: "artifact_manager",
            text: "Before the dataset lookup.",
          },
        },
        {
          type: "data-subagent-tool-input",
          id: "artifact_manager_tool_input",
          data: {
            parentToolCallId: "toolu_PARENT",
            subagentName: "artifact_manager",
            toolCallId: "artifact_manager_toolu_CHILD",
            toolName: "describe_dataset",
            input: { dataset: "visits" },
          },
        },
        {
          type: "data-subagent-tool-output",
          id: "artifact_manager_tool_output",
          data: {
            parentToolCallId: "toolu_PARENT",
            subagentName: "artifact_manager",
            toolCallId: "artifact_manager_toolu_CHILD",
            toolName: "describe_dataset",
            output: JSON.stringify({ name: "visits" }),
          },
        },
        {
          type: "data-subagent-text",
          id: "artifact_manager_text_after",
          data: {
            parentToolCallId: "toolu_PARENT",
            subagentName: "artifact_manager",
            text: "After the dataset lookup.",
          },
        },
      ],
    } as unknown as UIMessage

    render(<ChatMessage message={msg} isActiveMessage={false} />)

    fireEvent.click(screen.getByTestId("tool-call-artifact_manager"))
    const before = screen.getByText("Before the dataset lookup.")
    const tool = screen.getByTestId("tool-call-describe_dataset")
    const after = screen.getByText("After the dataset lookup.")

    expect(before.compareDocumentPosition(tool) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(tool.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
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

  it("does not show a view artifact button for artifact manager validation errors", () => {
    const msg = liveMessage(
      "artifact_manager",
      "Error invoking tool 'artifact_manager': task Field required. input contained artifact_id and subagent_event_queue metadata.",
    )

    render(<ChatMessage message={msg} isActiveMessage={false} />)

    expect(screen.getByTestId("tool-call-artifact_manager")).toBeInTheDocument()
    expect(screen.queryByText("View Artifact")).not.toBeInTheDocument()
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
    expect(screen.getByText("Starting Artifact editor...")).toBeInTheDocument()
  })
})

function helperMessage(state = "output-available"): UIMessage {
  return {
    id: "helpers", role: "assistant", parts: [
      { type: "tool-artifact_manager", toolCallId: "parent", state,
        input: { task: "Create charts" }, output: { status: "done", artifact_id: "a", artifact_version: 1 } },
      ...[
        { status: "created", artifact: { id: "a", version: 1 } },
        { status: "updated", artifact: { id: "a", version: 2 } },
        { status: "created", artifact: { id: "b", version: 1 } },
        { status: "error", artifact: { id: "failed", version: 1 } },
      ].map((output, i) => ({ type: "data-subagent-tool-output", data: {
        parentToolCallId: "parent", toolCallId: `child-${i}`, toolName: "artifact_write", output,
      } })),
      { type: "text", text: "Your charts are ready." },
    ],
  } as unknown as UIMessage
}

describe("helper artifacts in the assistant flow", () => {
  it("shows each published artifact after the collapsed helper, retaining its latest version", () => {
    render(<ChatMessage message={helperMessage()} isActiveMessage={false} />)
    const helper = screen.getByTestId("tool-call-artifact_manager")
    expect(helper).toHaveAttribute("aria-expanded", "false")
    const a = screen.getByTestId("chat-artifact-a")
    const b = screen.getByTestId("chat-artifact-b")
    expect(a).toHaveAttribute("data-artifact-version", "2")
    expect(screen.getAllByText("View Artifact")).toHaveLength(2)
    expect(screen.queryByTestId("chat-artifact-failed")).not.toBeInTheDocument()
    expect(helper.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(b.compareDocumentPosition(screen.getByText("Your charts are ready.")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(b)
    expect(useAppStore.getState().activeArtifactId).toBe("b")
    fireEvent.click(helper)
    expect(helper).toHaveAttribute("aria-expanded", "true")
  })

  it("keeps running helpers open, collapses at completion, and allows reopening", () => {
    const { rerender } = render(<ChatMessage message={helperMessage("input-available")} isActiveMessage={true} />)
    const helper = screen.getByTestId("tool-call-artifact_manager")
    expect(helper).toHaveAttribute("aria-expanded", "true")
    fireEvent.click(helper)
    fireEvent.click(helper)
    rerender(<ChatMessage message={helperMessage()} isActiveMessage={true} />)
    expect(helper).toHaveAttribute("aria-expanded", "false")
    fireEvent.click(helper)
    expect(helper).toHaveAttribute("aria-expanded", "true")
  })

  it("deduplicates artifacts referenced by multiple parent calls", () => {
    const message = helperMessage()
    message.parts.push({ ...message.parts[0], toolCallId: "other", output: { status: "done", artifact_id: "a", artifact_version: 3 } } as unknown as UIMessage["parts"][number])
    render(<ChatMessage message={message} isActiveMessage={false} />)
    expect(screen.getAllByTestId("chat-artifact-a")).toHaveLength(1)
    expect(screen.getByTestId("chat-artifact-a")).toHaveAttribute("data-artifact-version", "3")
  })
})

it.each(["Canvas Manager", "canvas manager", "canvas_manager"])("uses plain-language labels for %s and its activity", (label) => {
  const msg = liveMessage("canvas_manager", { status: "done", message: `${label} completed.` })
  render(<ChatMessage message={msg} isActiveMessage={false} />)
  expect(screen.getByText("Data model editor")).toBeInTheDocument()
  fireEvent.click(screen.getByTestId("tool-call-canvas_manager"))
  expect(screen.getByText("Data model editor completed.")).toBeInTheDocument()
  expect(screen.queryByText(/Canvas Manager/)).not.toBeInTheDocument()
})

it("navigates relative artifact markdown links through the client router", async () => {
  const msg = { id: "link", role: "assistant", parts: [{ type: "text", text: "[View chart](/workspaces/ws/artifacts/art)" }] } as UIMessage
  render(<MemoryRouter initialEntries={["/chat"]}><Routes>
    <Route path="/chat" element={<ChatMessage message={msg} isActiveMessage={false} />} />
    <Route path="/workspaces/ws/artifacts/art" element={<p>Opened chart</p>} />
  </Routes></MemoryRouter>)
  await userEvent.click(screen.getByRole("link", { name: "View chart" }))
  expect(await screen.findByText("Opened chart")).toBeInTheDocument()
})

it("deduplicates artifacts across assistant steps within each saved turn", () => {
  const first = helperMessage()
  const second = helperMessage()
  second.id = "second-step"
  const nextTurn = helperMessage()
  nextTurn.id = "next-turn"
  const messages = [first, second, { id: "question", role: "user", parts: [{ type: "text", text: "Update it" }] }, nextTurn] as UIMessage[]
  render(<ChatMessageList messages={messages} />)
  expect(screen.getAllByTestId("chat-artifact-a")).toHaveLength(2)
  expect(screen.getAllByTestId("chat-artifact-b")).toHaveLength(2)
})

function ChatMessageList({ messages }: { messages: UIMessage[] }) {
  const visible = turnArtifactOwners(messages)
  return messages.map((message) => <ChatMessage key={message.id} message={message} isActiveMessage={false} visibleArtifactIds={visible.get(message.id)} />)
}

it("does not leak markdown AST node props onto anchors", () => {
  const msg = { id: "link-node", role: "assistant", parts: [{ type: "text", text: "[Chart](/artifacts/a)" }] } as UIMessage
  render(<MemoryRouter><ChatMessage message={msg} isActiveMessage={false} /></MemoryRouter>)
  expect(screen.getByRole("link", { name: "Chart" })).not.toHaveAttribute("node")
})

it("preserves Markdown footnote navigation and accessibility attributes", () => {
  const msg = { id: "footnote", role: "assistant", parts: [{ type: "text", text: "Chart[^1]\n\n[^1]: Dataset details." }] } as UIMessage
  render(<MemoryRouter><ChatMessage message={msg} isActiveMessage={false} /></MemoryRouter>)
  const reference = screen.getByRole("link", { name: "1" })
  expect(reference).toHaveAttribute("id", "user-content-fnref-1")
  expect(reference).toHaveAttribute("aria-describedby", "footnote-label")
  expect(screen.getByRole("link", { name: "Back to reference 1" })).toHaveAttribute("href", "#user-content-fnref-1")
})

it("keeps helper errors visible at completion", () => {
  const message = { id: "failed-helper", role: "assistant", parts: [
    { type: "tool-artifact_manager", toolCallId: "failed", state: "output-error", input: {}, errorText: "The chart service timed out." },
  ] } as unknown as UIMessage
  render(<ChatMessage message={message} isActiveMessage={false} />)
  expect(screen.getByTestId("tool-call-artifact_manager")).toHaveAttribute("aria-expanded", "true")
  expect(screen.getByText("The chart service timed out.")).toBeVisible()
})

it("keeps failed helper summaries visible at completion", () => {
  const message = liveMessage("artifact_manager", { status: "error", message: "The chart could not be saved." })
  render(<ChatMessage message={message} isActiveMessage={false} />)
  expect(screen.getByTestId("tool-call-artifact_manager")).toHaveAttribute("aria-expanded", "true")
  expect(screen.getByText("The chart could not be saved.")).toBeVisible()
})

it.each(["blocked", "needs_data_model", "invalid_data_requirements"])("keeps %s helper outcomes visible", (status) => {
  const message = liveMessage("artifact_manager", { status, message: "Please resolve the data requirements." })
  render(<ChatMessage message={message} isActiveMessage={false} />)
  expect(screen.getByTestId("tool-call-artifact_manager")).toHaveAttribute("aria-expanded", "true")
  expect(screen.getByText("Please resolve the data requirements.")).toBeVisible()
})

it("shows plain-string helper validation errors", () => {
  const message = liveMessage("artifact_manager", "Please provide an artifact task.")
  render(<ChatMessage message={message} isActiveMessage={false} />)
  expect(screen.getByTestId("tool-call-artifact_manager")).toHaveAttribute("aria-expanded", "true")
  expect(screen.getByText("Please provide an artifact task.")).toBeVisible()
})

it("uses the UI helper label in plain validation errors", () => {
  const message = liveMessage("artifact_manager", "artifact_manager requires a non-empty task.")
  render(<ChatMessage message={message} isActiveMessage={false} />)
  expect(screen.getByText("Artifact editor requires a non-empty task.")).toBeVisible()
})

it("keeps earlier artifact tool activity when its button is deduplicated", () => {
  const messages = [1, 2].map((version) => ({ id: `read-${version}`, role: "assistant", parts: [
    { type: "tool-artifact_graph_overview", toolCallId: `overview-${version}`, state: "output-available", input: {},
      output: { status: "ok", artifact: { id: "shared-artifact", version } } },
  ] })) as UIMessage[]
  render(<ChatMessageList messages={messages} />)
  expect(screen.getAllByTestId("chat-artifact-shared-artifact")).toHaveLength(1)
  expect(screen.getByTestId("tool-call-artifact_graph_overview")).toBeInTheDocument()
})

it.each(["needs_data_model", "invalid_data_requirements"])("keeps published artifacts visible alongside %s handoffs", (status) => {
  const message = liveMessage("artifact_manager", { status, artifact_id: "published", ui_path: "/workspaces/w/artifacts/published" })
  render(<ChatMessage message={message} isActiveMessage={false} />)
  expect(screen.getByTestId("tool-call-artifact_manager")).toHaveAttribute("aria-expanded", "true")
  expect(screen.getByTestId("chat-artifact-published")).toBeInTheDocument()
})

describe("artifact link card title", () => {
  it("shows the artifact title on the card and keeps it across later versions", () => {
    const message = {
      id: "titled", role: "assistant", parts: [
        { type: "tool-artifact_write", toolCallId: "t1", state: "output-available", input: {},
          output: { status: "created", artifact: { id: "a", version: 1, title: "Sales dashboard" } } },
        { type: "tool-artifact_write", toolCallId: "t2", state: "output-available", input: {},
          output: { status: "updated", artifact: { id: "a", version: 2 } } },
      ],
    } as unknown as UIMessage
    render(<ChatMessage message={message} isActiveMessage={false} />)
    expect(screen.getByTestId("chat-artifact-title-a")).toHaveTextContent("Sales dashboard")
    expect(screen.getByTestId("chat-artifact-a")).toHaveAttribute("data-artifact-version", "2")
  })
})

describe("artifact link card from reloaded history", () => {
  it("reads artifact_title from a JSON-string manager output", () => {
    const message = {
      id: "reload", role: "assistant", parts: [
        { type: "tool-artifact_manager", toolCallId: "p", state: "output-available", input: {},
          output: JSON.stringify({ status: "done", artifact_id: "a", artifact_version: 1, artifact_title: "Sales dashboard" }) },
      ],
    } as unknown as UIMessage
    render(<ChatMessage message={message} isActiveMessage={false} />)
    expect(screen.getByTestId("chat-artifact-title-a")).toHaveTextContent("Sales dashboard")
  })

  it("falls back to the untitled card", () => {
    const message = {
      id: "legacy", role: "assistant", parts: [
        { type: "tool-artifact_write", toolCallId: "t", state: "output-available", input: {},
          output: { status: "created", artifact: { id: "a", version: 1 } } },
      ],
    } as unknown as UIMessage
    render(<ChatMessage message={message} isActiveMessage={false} />)
    expect(screen.queryByTestId("chat-artifact-title-a")).not.toBeInTheDocument()
    expect(screen.getByTestId("chat-artifact-a")).toHaveAttribute("aria-label", "View Artifact")
  })
})
