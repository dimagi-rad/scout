import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen } from "@testing-library/react"
import { useRef } from "react"

import { QuoteSelectionAction } from "./QuoteSelectionAction"

function Harness({ onQuote }: { onQuote: (text: string) => void }) {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <div ref={ref}>
      <div data-quotable="true" data-testid="reply">
        <p>Visits fell 20% in May.</p>
        <p>Two sites closed.</p>
      </div>
      <div data-testid="user">My own question</div>
      <QuoteSelectionAction containerRef={ref} onQuote={onQuote} />
    </div>
  )
}

function select(node: Node, start: number, endNode: Node, end: number) {
  const range = document.createRange()
  range.setStart(node, start)
  range.setEnd(endNode, end)
  const selection = window.getSelection()!
  selection.removeAllRanges()
  selection.addRange(range)
  act(() => {
    document.dispatchEvent(new MouseEvent("mouseup"))
  })
}

const originalRect = Range.prototype.getBoundingClientRect
const originalElementRect = Element.prototype.getBoundingClientRect

describe("QuoteSelectionAction", () => {
  beforeEach(() => {
    Range.prototype.getBoundingClientRect = () => new DOMRect(100, 200, 80, 16)
    Element.prototype.getBoundingClientRect = () => new DOMRect(0, 0, 1000, 1000)
  })
  afterEach(() => {
    Range.prototype.getBoundingClientRect = originalRect
    Element.prototype.getBoundingClientRect = originalElementRect
    window.getSelection()?.removeAllRanges()
  })

  it("offers to quote text selected in a Scout reply", () => {
    const onQuote = vi.fn()
    render(<Harness onQuote={onQuote} />)
    const text = screen.getByText("Visits fell 20% in May.").firstChild!
    select(text, 0, text, 11)

    const action = screen.getByTestId("chat-quote-action")
    fireEvent.click(action)

    expect(onQuote).toHaveBeenCalledWith("Visits fell")
    expect(screen.queryByTestId("chat-quote-action")).not.toBeInTheDocument()
    expect(window.getSelection()?.isCollapsed).toBe(true)
  })

  it("does not offer to quote text outside a Scout reply", () => {
    render(<Harness onQuote={vi.fn()} />)
    const text = screen.getByTestId("user").firstChild!
    select(text, 0, text, 6)
    expect(screen.queryByTestId("chat-quote-action")).not.toBeInTheDocument()
  })

  it("does not offer a selection that runs past the reply", () => {
    render(<Harness onQuote={vi.fn()} />)
    const start = screen.getByText("Two sites closed.").firstChild!
    const end = screen.getByTestId("user").firstChild!
    select(start, 0, end, 6)
    expect(screen.queryByTestId("chat-quote-action")).not.toBeInTheDocument()
  })
})
