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
const BUTTON_HALF_WIDTH_PX = 40

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
  const visible = container.getBoundingClientRect()
  // Scrolled out of the transcript: don't pin the button over the header or composer.
  if (rect.bottom < visible.top || rect.top > visible.bottom) return null
  const center = rect.left + rect.width / 2
  return {
    top: Math.max(visible.top + 4, rect.top - BUTTON_OFFSET_PX),
    left: Math.min(Math.max(center, visible.left + BUTTON_HALF_WIDTH_PX), visible.right - BUTTON_HALF_WIDTH_PX),
    text,
  }
}

/** A floating "Quote" button over text selected in an assistant reply. */
export function QuoteSelectionAction({ containerRef, onQuote }: QuoteSelectionActionProps) {
  const [anchor, setAnchor] = useState<Anchor | null>(null)

  useEffect(() => {
    const container = containerRef.current
    // Shown only once the selection settles, so it doesn't chase a drag.
    const show = (event: Event) => {
      if (event instanceof KeyboardEvent && event.key === "Escape") {
        setAnchor(null)
        return
      }
      setAnchor(readSelection(containerRef.current))
    }
    // selectionchange fires on every step of a drag, so this skips readSelection's layout reads.
    const hideIfCleared = () => {
      const selection = window.getSelection()
      if (!selection || selection.isCollapsed) setAnchor(null)
    }
    const reposition = () => setAnchor((current) => (current ? readSelection(containerRef.current) : null))
    document.addEventListener("mouseup", show)
    document.addEventListener("touchend", show)
    document.addEventListener("keyup", show)
    document.addEventListener("selectionchange", hideIfCleared)
    container?.addEventListener("scroll", reposition)
    window.addEventListener("resize", reposition)
    return () => {
      document.removeEventListener("mouseup", show)
      document.removeEventListener("touchend", show)
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
