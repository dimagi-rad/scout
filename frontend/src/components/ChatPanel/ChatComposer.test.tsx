import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"

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
