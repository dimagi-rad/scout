import { useLayoutEffect, type KeyboardEvent, type RefObject } from "react"

/** apps/chat/constants.py MAX_MESSAGE_LENGTH; the server refuses longer messages. */
export const MAX_MESSAGE_LENGTH = 10_000
export const CHAR_COUNT_VISIBLE_FROM = MAX_MESSAGE_LENGTH * 0.8

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

/** Grows a textarea with its content up to a cap, then it scrolls. */
export function useAutoGrow(ref: RefObject<HTMLTextAreaElement | null>, value: string) {
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const previous = el.style.height
    el.style.height = "auto"
    // Hidden (display: none) measures 0; keep the last size rather than collapse it.
    if (!el.scrollHeight) {
      el.style.height = previous
      return
    }
    el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_HEIGHT_PX)}px`
    el.style.overflowY = el.scrollHeight > MAX_TEXTAREA_HEIGHT_PX ? "auto" : "hidden"
  }, [ref, value])
}
