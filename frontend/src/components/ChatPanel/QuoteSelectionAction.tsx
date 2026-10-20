import { useEffect, useState, type RefObject } from "react"
import { createPortal } from "react-dom"
import { Quote } from "lucide-react"

interface QuoteSelectionActionProps {
  /** Only selections inside this element are quotable. */
  containerRef: RefObject<HTMLElement | null>
  onQuote: (text: string) => void
}

interface Anchor {
  top: number
  left: number
  text: string
}

const BUTTON_OFFSET_PX = 36

function quotableRoot(node: Node | null): Element | null {
  const el = node instanceof Element ? node : node?.parentElement
  return el?.closest("[data-quotable]") ?? null
}

/** The quotable selection, when it lies within one Scout response's text. */
function readSelection(container: HTMLElement | null): Anchor | null {
  const selection = window.getSelection()
  if (!container || !selection || selection.isCollapsed || selection.rangeCount === 0) return null
  const text = selection.toString().trim()
  if (!text) return null
  const root = quotableRoot(selection.anchorNode)
  if (!root || root !== quotableRoot(selection.focusNode) || !container.contains(root)) return null
  const rect = selection.getRangeAt(0).getBoundingClientRect()
  return {
    top: Math.max(8, rect.top - BUTTON_OFFSET_PX),
    left: rect.left + rect.width / 2,
    text,
  }
}

/** A floating "Quote" button over text selected in an assistant reply. */
export function QuoteSelectionAction({ containerRef, onQuote }: QuoteSelectionActionProps) {
  const [anchor, setAnchor] = useState<Anchor | null>(null)

  useEffect(() => {
    const container = containerRef.current
    // Shown only once the selection settles, so it doesn't chase a drag.
    const show = () => setAnchor(readSelection(containerRef.current))
    const hideIfCleared = () => {
      if (!readSelection(containerRef.current)) setAnchor(null)
    }
    const reposition = () => setAnchor((current) => (current ? readSelection(containerRef.current) : null))
    document.addEventListener("mouseup", show)
    document.addEventListener("keyup", show)
    document.addEventListener("selectionchange", hideIfCleared)
    container?.addEventListener("scroll", reposition)
    window.addEventListener("resize", reposition)
    return () => {
      document.removeEventListener("mouseup", show)
      document.removeEventListener("keyup", show)
      document.removeEventListener("selectionchange", hideIfCleared)
      container?.removeEventListener("scroll", reposition)
      window.removeEventListener("resize", reposition)
    }
  }, [containerRef])

  if (!anchor) return null

  return createPortal(
    <button
      type="button"
      data-testid="chat-quote-action"
      className="fixed z-50 flex -translate-x-1/2 items-center gap-1 rounded-md border bg-popover px-2 py-1 text-xs font-medium text-popover-foreground shadow-md hover:bg-accent"
      style={{ top: anchor.top, left: anchor.left }}
      // Keep the selection alive until the click lands.
      onMouseDown={(e) => e.preventDefault()}
      onClick={() => {
        onQuote(anchor.text)
        window.getSelection()?.removeAllRanges()
        setAnchor(null)
      }}
    >
      <Quote className="h-3 w-3" aria-hidden="true" />
      Quote
    </button>,
    document.body,
  )
}
