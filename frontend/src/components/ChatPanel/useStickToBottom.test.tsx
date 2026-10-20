import { useRef } from "react"
import { describe, expect, it } from "vitest"
import { act, fireEvent, render, screen } from "@testing-library/react"
import { useStickToBottom } from "./useStickToBottom"

function Harness({ content, resetKey = "a", forceKey = "x" }: { content: string; resetKey?: string; forceKey?: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const { onScroll, onWheel, hasNewBelow, jumpToBottom } = useStickToBottom(ref, content, "", resetKey, forceKey)
  return (
    <div>
      <div ref={ref} onScroll={onScroll} onWheel={onWheel} data-testid="scroller">{content}</div>
      {hasNewBelow && <button onClick={jumpToBottom} data-testid="jump">jump</button>}
    </div>
  )
}

function layout(el: HTMLElement, scrollTop: number) {
  Object.defineProperty(el, "scrollHeight", { configurable: true, value: 1000 })
  Object.defineProperty(el, "clientHeight", { configurable: true, value: 400 })
  el.scrollTop = scrollTop
  fireEvent.scroll(el)
}

describe("useStickToBottom", () => {
  it("follows new content while at the bottom", () => {
    const { rerender } = render(<Harness content="one" />)
    const el = screen.getByTestId("scroller")
    layout(el, 600)
    rerender(<Harness content="two" />)
    expect(el.scrollTop).toBe(1000)
    expect(screen.queryByTestId("jump")).not.toBeInTheDocument()
  })

  it("leaves the scroll position alone once the user scrolls up, and offers a jump", () => {
    const { rerender } = render(<Harness content="one" />)
    const el = screen.getByTestId("scroller")
    layout(el, 100)
    expect(screen.queryByTestId("jump")).not.toBeInTheDocument()
    rerender(<Harness content="two" />)
    expect(el.scrollTop).toBe(100)
    expect(screen.getByTestId("jump")).toBeInTheDocument()

    fireEvent.click(screen.getByTestId("jump"))
    expect(el.scrollTop).toBe(1000)
    expect(screen.queryByTestId("jump")).not.toBeInTheDocument()
  })

  it("detaches on a small upward scroll from a pinned view", () => {
    const { rerender } = render(<Harness content="one" />)
    const el = screen.getByTestId("scroller")
    layout(el, 600)
    rerender(<Harness content="two" />)
    act(() => layout(el, 560))
    rerender(<Harness content="three" />)
    expect(el.scrollTop).toBe(560)
    expect(screen.getByTestId("jump")).toBeInTheDocument()
  })

  it("detaches on an upward wheel before any scroll event arrives", () => {
    const { rerender } = render(<Harness content="one" />)
    const el = screen.getByTestId("scroller")
    layout(el, 600)
    fireEvent.wheel(el, { deltaY: -20 })
    rerender(<Harness content="two" />)
    expect(el.scrollTop).toBe(600)
    expect(screen.getByTestId("jump")).toBeInTheDocument()
  })

  it("resumes following when the user scrolls back to the bottom", () => {
    const { rerender } = render(<Harness content="one" />)
    const el = screen.getByTestId("scroller")
    layout(el, 100)
    rerender(<Harness content="two" />)
    act(() => layout(el, 600))
    expect(screen.queryByTestId("jump")).not.toBeInTheDocument()
    rerender(<Harness content="three" />)
    expect(el.scrollTop).toBe(1000)
  })

  it("re-attaches when the user sends or the conversation changes", () => {
    const { rerender } = render(<Harness content="one" />)
    const el = screen.getByTestId("scroller")
    layout(el, 100)
    rerender(<Harness content="two" forceKey="y" />)
    expect(el.scrollTop).toBe(1000)
    layout(el, 100)
    rerender(<Harness content="three" forceKey="y" resetKey="b" />)
    expect(el.scrollTop).toBe(1000)
  })
})
