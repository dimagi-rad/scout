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

  it("stays typeable and does not send on Enter while streaming", () => {
    const onSend = vi.fn()
    const setInput = vi.fn()
    render(
      <ChatComposer
        input="queued thought"
        setInput={setInput}
        onSend={onSend}
        isStreaming
        onStop={vi.fn()}
      />,
    )
    const field = screen.getByTestId("chat-input")
    expect(field).not.toBeDisabled()

    fireEvent.change(field, { target: { value: "queued thoughts" } })
    expect(setInput).toHaveBeenCalledWith("queued thoughts")
    setInput.mockClear()

    fireEvent.keyDown(field, { key: "Enter" })
    expect(onSend).not.toHaveBeenCalled()
    expect(setInput).not.toHaveBeenCalled()
  })

  it("keeps the empty-state prompt typeable but unsendable while streaming", () => {
    const onSend = vi.fn()
    const setInput = vi.fn()
    render(<ChatEmptyPrompt input="hi" setInput={setInput} onSend={onSend} disabled />)
    const field = screen.getByTestId("chat-input-prominent")
    expect(field).not.toBeDisabled()
    fireEvent.keyDown(field, { key: "Enter" })
    expect(onSend).not.toHaveBeenCalled()
    expect(setInput).not.toHaveBeenCalled()
  })

  it("keeps Stop working while streaming", () => {
    const onStop = vi.fn()
    render(
      <ChatComposer input="" setInput={vi.fn()} onSend={vi.fn()} isStreaming onStop={onStop} />,
    )
    fireEvent.click(screen.getByTestId("chat-stop"))
    expect(onStop).toHaveBeenCalledTimes(1)
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

  it("does not send on Enter while an IME composition is active", () => {
    const onSend = vi.fn()
    render(<ChatComposer input="こんにちは" setInput={vi.fn()} onSend={onSend} />)

    fireEvent.keyDown(screen.getByTestId("chat-input"), { key: "Enter", isComposing: true })

    expect(onSend).not.toHaveBeenCalled()
  })

  it("leaves Shift+Enter's default newline insertion alone", () => {
    render(<ChatComposer input="First line" setInput={vi.fn()} onSend={vi.fn()} />)

    const notPrevented = fireEvent.keyDown(screen.getByTestId("chat-input"), {
      key: "Enter",
      shiftKey: true,
    })

    expect(notPrevented).toBe(true)
    expect(screen.getByTestId("chat-input").tagName).toBe("TEXTAREA")
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

describe("ChatComposer quotes (#920)", () => {
  it("shows each quote as a removable chip", () => {
    const onRemoveQuote = vi.fn()
    render(
      <ChatComposer
        input=""
        setInput={vi.fn()}
        onSend={vi.fn()}
        quotes={["first point", "second point"]}
        onRemoveQuote={onRemoveQuote}
      />,
    )
    const chips = screen.getAllByTestId("composer-quote-chip")
    expect(chips.map((c) => c.textContent)).toEqual(["first point", "second point"])
    fireEvent.click(screen.getAllByTestId("composer-quote-remove")[1])
    expect(onRemoveQuote).toHaveBeenCalledWith(1)
  })

  it("sends the quotes ahead of the reply as blockquotes and clears them", () => {
    const onSend = vi.fn()
    const onQuotesSent = vi.fn()
    render(
      <ChatComposer
        input="Why did that happen?"
        setInput={vi.fn()}
        onSend={onSend}
        quotes={["visits fell", "two sites\nclosed"]}
        onQuotesSent={onQuotesSent}
      />,
    )
    fireEvent.keyDown(screen.getByTestId("chat-input"), { key: "Enter" })
    expect(onSend).toHaveBeenCalledWith("> visits fell\n\n> two sites\n> closed\n\nWhy did that happen?")
    expect(onQuotesSent).toHaveBeenCalled()
  })

  it("focuses the input when a quote is picked, not when chips just appear", () => {
    const props = { input: "", setInput: vi.fn(), onSend: vi.fn() }
    const { rerender } = render(<ChatComposer {...props} quotes={[]} />)
    // As on switching to a chat that already holds quotes.
    rerender(<ChatComposer {...props} quotes={["a point"]} />)
    expect(screen.getByTestId("chat-input")).not.toHaveFocus()
    rerender(<ChatComposer {...props} quotes={["a point", "another"]} focusRequest={1} />)
    expect(screen.getByTestId("chat-input")).toHaveFocus()
  })

  it("does not refocus on a remount for a pick it already handled", () => {
    render(<ChatComposer input="" setInput={vi.fn()} onSend={vi.fn()} focusRequest={3} />)
    expect(screen.getByTestId("chat-input")).not.toHaveFocus()
  })
})

describe("ChatComposer multi-line input and length limit (#913)", () => {
  it("is a multi-line textarea", () => {
    render(<ChatComposer input="" setInput={vi.fn()} onSend={vi.fn()} />)
    expect(screen.getByTestId("chat-input").tagName).toBe("TEXTAREA")
  })

  it("does not send on Enter while an IME is composing", () => {
    const onSend = vi.fn()
    render(<ChatComposer input="こんにちは" setInput={vi.fn()} onSend={onSend} />)
    const field = screen.getByTestId("chat-input")
    fireEvent.keyDown(field, { key: "Enter", isComposing: true })
    fireEvent.keyDown(field, { key: "Enter", keyCode: 229 })
    expect(onSend).not.toHaveBeenCalled()
    fireEvent.keyDown(field, { key: "Enter" })
    expect(onSend).toHaveBeenCalledWith("こんにちは")
  })

  it("hides the counter below 8,000 characters", () => {
    render(<ChatComposer input={"a".repeat(7_999)} setInput={vi.fn()} onSend={vi.fn()} />)
    expect(screen.queryByTestId("chat-char-count")).toBeNull()
  })

  it("shows the counter from 8,000 characters and still sends at 10,000", () => {
    const onSend = vi.fn()
    const { rerender } = render(
      <ChatComposer input={"a".repeat(8_000)} setInput={vi.fn()} onSend={onSend} />,
    )
    expect(screen.getByTestId("chat-char-count")).toHaveTextContent("8,000 / 10,000")
    expect(screen.getByTestId("chat-char-count")).not.toHaveAttribute("data-over-limit")

    rerender(<ChatComposer input={"a".repeat(10_000)} setInput={vi.fn()} onSend={onSend} />)
    expect(screen.getByTestId("chat-send")).not.toBeDisabled()
    fireEvent.keyDown(screen.getByTestId("chat-input"), { key: "Enter" })
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it("turns red and blocks the send over 10,000 characters", () => {
    const onSend = vi.fn()
    render(<ChatComposer input={"a".repeat(10_001)} setInput={vi.fn()} onSend={onSend} />)
    const counter = screen.getByTestId("chat-char-count")
    expect(counter).toHaveAttribute("data-over-limit", "true")
    expect(counter).toHaveClass("text-destructive")
    expect(screen.getByTestId("chat-char-count-hint")).toHaveTextContent("Shorten it by 1 character.")
    expect(screen.getByTestId("chat-send")).toBeDisabled()
    fireEvent.keyDown(screen.getByTestId("chat-input"), { key: "Enter" })
    expect(onSend).not.toHaveBeenCalled()
  })

  it("counts the quotes that go out with the reply", () => {
    render(
      <ChatComposer
        input={"a".repeat(7_000)}
        setInput={vi.fn()}
        onSend={vi.fn()}
        quotes={["q".repeat(1_500)]}
      />,
    )
    // "> " + quote + "\n\n" + reply
    expect(screen.getByTestId("chat-char-count")).toHaveTextContent("8,504 / 10,000")
  })

  it("counts a held request's text that the message joins", () => {
    const onSend = vi.fn()
    render(
      <ChatComposer
        input={"a".repeat(2_000)}
        setInput={vi.fn()}
        onSend={onSend}
        mode="add"
        heldText={"h".repeat(9_000)}
      />,
    )
    // held + "\n\n" + typed
    expect(screen.getByTestId("chat-char-count")).toHaveTextContent("11,002 / 10,000")
    expect(screen.getByTestId("chat-char-count-hint")).toHaveTextContent(
      "Together with your held request this is 1,002 characters too long. Shorten it, or discard the request.",
    )
    expect(screen.getByTestId("chat-add-to-request")).toBeDisabled()
  })

  it("describes the field with the counter, and announces only going over the limit", () => {
    const { container, rerender } = render(
      <ChatComposer input={"a".repeat(9_000)} setInput={vi.fn()} onSend={vi.fn()} />,
    )
    const field = screen.getByTestId("chat-input")
    const described = document.getElementById(field.getAttribute("aria-describedby")!)
    expect(described).toContainElement(screen.getByTestId("chat-char-count"))
    const live = container.querySelector("[aria-live=polite]")!
    expect(live).toHaveTextContent(/^$/)

    rerender(<ChatComposer input={"a".repeat(10_001)} setInput={vi.fn()} onSend={vi.fn()} />)
    expect(described).toContainElement(screen.getByTestId("chat-char-count-hint"))
    expect(live).toHaveTextContent("Message is too long to send.")
  })

  it("counts characters as the server does, not UTF-16 units", () => {
    render(<ChatComposer input={"😀".repeat(8_000)} setInput={vi.fn()} onSend={vi.fn()} />)
    expect(screen.getByTestId("chat-char-count")).toHaveTextContent("8,000 / 10,000")
  })

  it("blocks an over-long send from the empty-state prompt", () => {
    const onSend = vi.fn()
    render(<ChatEmptyPrompt input={"a".repeat(10_001)} setInput={vi.fn()} onSend={onSend} />)
    expect(screen.getByTestId("chat-char-count")).toHaveAttribute("data-over-limit", "true")
    fireEvent.keyDown(screen.getByTestId("chat-input-prominent"), { key: "Enter" })
    expect(onSend).not.toHaveBeenCalled()
  })
})
