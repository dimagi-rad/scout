import { useCallback, useEffect, useRef, useState, type RefObject } from "react"

// Re-attach only at the true bottom; any user scroll away from a pinned view detaches.
const BOTTOM_THRESHOLD_PX = 8

interface Detached {
  content: unknown
  streamingText: unknown
  resetKey: unknown
  forceKey: unknown
}

/**
 * Follows new content only while the user is at the bottom. Scrolling up
 * detaches; `hasNewBelow` then flags content that arrived since. Returning to
 * the bottom or calling `jumpToBottom` re-attaches. A change of `resetKey`
 * (conversation switched) or `forceKey` (user sent) also re-attaches.
 */
export function useStickToBottom(
  ref: RefObject<HTMLDivElement | null>,
  content: unknown,
  streamingText: unknown,
  resetKey: unknown,
  forceKey: unknown,
) {
  const [detached, setDetached] = useState<Detached | null>(null)
  // Drop a snapshot from another conversation or send so it can't match again later.
  if (detached && (detached.resetKey !== resetKey || detached.forceKey !== forceKey)) {
    setDetached(null)
  }
  const current = detached?.resetKey === resetKey && detached?.forceKey === forceKey ? detached : null
  const isStuck = current === null
  const hasNewBelow =
    current !== null && (current.content !== content || current.streamingText !== streamingText)

  const lastPinned = useRef<number | null>(null)

  useEffect(() => {
    const el = ref.current
    if (isStuck && el) {
      el.scrollTop = el.scrollHeight
      lastPinned.current = el.scrollTop
    }
  }, [ref, isStuck, content, streamingText, resetKey, forceKey])

  const onScroll = () => {
    const el = ref.current
    if (!el) return
    // Our own pin fires a scroll event; it must not count as the user's position.
    if (isStuck && el.scrollTop === lastPinned.current) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_THRESHOLD_PX
    if (atBottom) setDetached(null)
    else setDetached((prev) => (prev && current ? prev : { content, streamingText, resetKey, forceKey }))
  }

  const jumpToBottom = useCallback(() => {
    setDetached(null)
    const el = ref.current
    if (el) el.scrollTop = el.scrollHeight
  }, [ref])

  return { onScroll, hasNewBelow, jumpToBottom }
}
