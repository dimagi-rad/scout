import { beforeEach, describe, expect, it, vi } from "vitest"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { personalMemoryApi } from "@/api/memory"
import type { UIMessage } from "ai"
import { MemoryRouter } from "react-router-dom"
import { ChatMessage } from "./ChatMessage"

vi.mock("@/api/memory", () => ({ personalMemoryApi: { remove: vi.fn() } }))

function memoryMessage(output: unknown, state = "output-available"): UIMessage {
  return {
    id: "m1",
    role: "assistant",
    parts: [
      {
        type: "tool-save_personal_memory",
        toolName: "save_personal_memory",
        toolCallId: "toolu_MEM",
        state,
        input: { memory: "Show district totals as a table" },
        output: JSON.stringify(output),
      },
    ],
  } as unknown as UIMessage
}

function renderMessage(message: UIMessage, path = "/") {
  render(
    <MemoryRouter initialEntries={[path]}>
      <ChatMessage message={message} isActiveMessage={true} />
    </MemoryRouter>,
  )
}

describe("Saved to memory chip", () => {
  beforeEach(() => vi.mocked(personalMemoryApi.remove).mockReset())

  it("undoes a new save in one click", async () => {
    vi.mocked(personalMemoryApi.remove).mockResolvedValue(undefined)
    renderMessage(
      memoryMessage({ status: "saved", layer: "personal", memory_id: "m1", memory: "Use tables" }),
    )
    await userEvent.click(screen.getByTestId("memory-saved-chip-undo"))

    expect(personalMemoryApi.remove).toHaveBeenCalledWith("m1")
    expect(await screen.findByText("Removed from memory", { exact: false })).toBeInTheDocument()
    expect(screen.queryByTestId("memory-saved-chip-undo")).not.toBeInTheDocument()
  })

  it("offers no undo for a memory that already existed", () => {
    renderMessage(
      memoryMessage({
        status: "already_saved",
        layer: "personal",
        memory_id: "m1",
        memory: "Use tables",
      }),
    )
    expect(screen.queryByTestId("memory-saved-chip-undo")).not.toBeInTheDocument()
  })

  it("names the layer and the saved text, and links to the Memory page", () => {
    renderMessage(
      memoryMessage({
        status: "saved",
        layer: "personal",
        memory: "Show district totals as a table",
      }),
    )
    const chip = screen.getByTestId("memory-saved-chip")
    expect(chip).toHaveTextContent("Saved to memory")
    expect(screen.getByTestId("memory-saved-chip-layer")).toHaveTextContent("Personal")
    expect(chip).toHaveTextContent("Show district totals as a table")
    expect(screen.getByTestId("memory-saved-chip-link")).toHaveAttribute("href", "/memory")
    expect(screen.queryByTestId("tool-call-save_personal_memory")).not.toBeInTheDocument()
  })

  it("keeps the embed prefix on the Memory link", () => {
    renderMessage(
      memoryMessage({ status: "already_saved", layer: "personal", memory: "Use metric units" }),
      "/embed/workspaces/ws/chat",
    )
    expect(screen.getByTestId("memory-saved-chip-link")).toHaveAttribute("href", "/embed/memory")
  })

  it("falls back to the tool card when nothing was saved", () => {
    renderMessage(memoryMessage({ status: "error", layer: "personal", message: "Too short" }))
    expect(screen.queryByTestId("memory-saved-chip")).not.toBeInTheDocument()
    expect(screen.getByTestId("tool-call-save_personal_memory")).toBeInTheDocument()
  })
})
