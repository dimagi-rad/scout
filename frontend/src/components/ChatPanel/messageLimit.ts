import { useEffect, useLayoutEffect, type KeyboardEvent, type RefObject } from "react"

/** apps/chat/constants.py MAX_MESSAGE_LENGTH; the server refuses longer messages. */
export const MAX_MESSAGE_LENGTH = 10_000
export const CHAR_COUNT_VISIBLE_FROM = Math.round(MAX_MESSAGE_LENGTH * 0.8)

/** Characters as the server counts them: Python's len() counts code points, not UTF-16 units. */
export function messageLength(text: string): number {
  return Array.from(text).length
}

export function isOverLimit(length: number): boolean {
  return length > MAX_MESSAGE_LENGTH
}

/** Enter while an IME is composing confirms the candidate; it must not send. */
export function isComposingKey(e: KeyboardEvent): boolean {
  // keyCode 229 is Safari's composition keydown, which reports isComposing=false.
  return e.nativeEvent.isComposing || e.keyCode === 229
}

const MAX_TEXTAREA_HEIGHT_PX = 240

function fitHeight(el: HTMLTextAreaElement) {
  const previous = el.style.height
  el.style.height = "auto"
  // Hidden (display: none) measures 0; keep the last size rather than collapse it.
  if (!el.scrollHeight) {
    el.style.height = previous
    return
  }
  // scrollHeight leaves out the border, which a border-box height includes.
  const border = el.offsetHeight - el.clientHeight
  const wanted = el.scrollHeight + border
  el.style.height = `${Math.min(wanted, MAX_TEXTAREA_HEIGHT_PX)}px`
  el.style.overflowY = wanted > MAX_TEXTAREA_HEIGHT_PX ? "auto" : "hidden"
}

/**
 * Grows a textarea with its content up to a cap, then it scrolls. Done in JS rather
 * than with CSS field-sizing, which Firefox does not support.
 */
export function useAutoGrow(ref: RefObject<HTMLTextAreaElement | null>, value: string) {
  useLayoutEffect(() => {
    if (ref.current) fitHeight(ref.current)
  }, [ref, value])

  // Lines rewrap when the width changes (a side panel opens, the window resizes).
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === "undefined") return
    let width = el.clientWidth
    let frame = 0
    // Resizing the element inside its own observer callback would loop
    // ("ResizeObserver loop completed"); the next frame is soon enough.
    const observer = new ResizeObserver(() => {
      if (el.clientWidth === width) return
      width = el.clientWidth
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => fitHeight(el))
    })
    observer.observe(el)
    return () => {
      observer.disconnect()
      cancelAnimationFrame(frame)
    }
  }, [ref])
}
