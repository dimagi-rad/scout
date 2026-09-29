import { afterEach, describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"

import type { WorkspaceRole } from "@/api/workspaces"
import { ChatEmptyPrompt } from "@/components/ChatEmptyState/ChatEmptyState"
import { useAppStore } from "@/store/store"
import { ChatComposer } from "./ChatComposer"

describe("ChatComposer", () => {
  it("submits a follow-up message with Enter", () => {
    const onSend = vi.fn()
    const setInput = vi.fn()
    render(
      <ChatComposer
        input="Show me visit totals"
        setInput={setInput}
        onSend={onSend}
      />,
    )

    fireEvent.keyDown(screen.getByTestId("chat-input"), { key: "Enter" })

    expect(onSend).toHaveBeenCalledWith("Show me visit totals")
    expect(setInput).toHaveBeenCalledWith("")
  })

  it("keeps Shift+Enter from submitting", () => {
    const onSend = vi.fn()
    render(
      <ChatComposer
        input="First line"
        setInput={vi.fn()}
        onSend={onSend}
      />,
    )

    fireEvent.keyDown(screen.getByTestId("chat-input"), {
      key: "Enter",
      shiftKey: true,
    })

    expect(onSend).not.toHaveBeenCalled()
  })

  it("names the send and stop controls for assistive technology", () => {
    const { rerender } = render(
      <ChatComposer input="Hi" setInput={vi.fn()} onSend={vi.fn()} />,
    )
    expect(screen.getByRole("button", { name: "Send message" })).toBeInTheDocument()

    rerender(
      <ChatComposer
        input=""
        setInput={vi.fn()}
        onSend={vi.fn()}
        isStreaming
        onStop={vi.fn()}
      />,
    )
    expect(screen.getByRole("button", { name: "Stop response" })).toBeInTheDocument()
  })
})

describe.each([
  ["ChatComposer", (input: string, onSend: (text: string) => void) => (
    <ChatComposer input={input} setInput={vi.fn()} onSend={onSend} />
  ), "chat-input"],
  ["ChatEmptyPrompt", (input: string, onSend: (text: string) => void) => (
    <ChatEmptyPrompt input={input} setInput={vi.fn()} onSend={onSend} />
  ), "chat-input-prominent"],
] as const)("%s slash menu role gating (FU 22)", (_name, renderInput, inputTestId) => {
  function seedRole(role: WorkspaceRole) {
    useAppStore.setState({
      activeDomainId: "ws",
      domains: [{
        id: "ws", name: "ws", display_name: "ws", is_auto_created: false, role, tenants: [],
        member_count: 1, schema_status: "available", last_synced_at: null, created_at: "2026-01-01T00:00:00Z",
      }],
    })
  }

  afterEach(() => {
    useAppStore.setState({ activeDomainId: null, domains: [] })
  })

  it("offers the write commands to a read-write member", () => {
    seedRole("read_write")
    render(renderInput("/", vi.fn()))
    expect(screen.getByTestId("slash-command-save-recipe")).toBeInTheDocument()
    expect(screen.getByTestId("slash-command-refresh-data")).toBeInTheDocument()
  })

  it("does not offer write commands to a read-only member", () => {
    seedRole("read")
    render(renderInput("/", vi.fn()))
    expect(screen.queryByTestId("slash-command-menu")).toBeNull()
    expect(screen.queryByTestId("slash-command-save-recipe")).toBeNull()
    expect(screen.queryByTestId("slash-command-refresh-data")).toBeNull()
  })

  it("lets Enter send, rather than pick a hidden command, for a read-only member", () => {
    seedRole("read")
    const onSend = vi.fn()
    render(renderInput("/re", onSend))
    fireEvent.keyDown(screen.getByTestId(inputTestId), { key: "Enter" })
    expect(onSend).toHaveBeenCalledWith("/re")
  })
})
