import { useCallback, useEffect, useState, type RefObject } from "react"

const BOTTOM_THRESHOLD_PX = 80

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
  const current = detached?.resetKey === resetKey && detached?.forceKey === forceKey ? detached : null
  const isStuck = current === null
  const hasNewBelow =
    current !== null && (current.content !== content || current.streamingText !== streamingText)

  useEffect(() => {
    const el = ref.current
    if (isStuck && el) el.scrollTop = el.scrollHeight
  }, [ref, isStuck, content, streamingText, resetKey, forceKey])

  const onScroll = () => {
    const el = ref.current
    if (!el) return
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
