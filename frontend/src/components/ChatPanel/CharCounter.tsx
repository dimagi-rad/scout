import { cn } from "@/lib/utils"

import { CHAR_COUNT_VISIBLE_FROM, MAX_MESSAGE_LENGTH, isOverLimit } from "./messageLimit"

function hintText(excess: number, includesHeld: boolean, suggestAttach: boolean): string {
  const amount = `${excess.toLocaleString()} ${excess === 1 ? "character" : "characters"}`
  if (includesHeld) {
    return `Together with your held request this is ${amount} too long. Shorten it, or discard the request.`
  }
  return `Too long to send. Shorten it by ${amount}${suggestAttach ? ", or attach the text as a file" : ""}.`
}

/**
 * The field points at this with aria-describedby, so it stays mounted. Going over
 * the limit is announced from a region that is always in the accessibility tree: one
 * added together with its text is often not read. The details stay in the
 * description, so they are not repeated on every keystroke.
 */
export function CharCounter({
  id,
  length,
  includesHeld = false,
  suggestAttach = false,
  className,
}: {
  id: string
  length: number
  /** The count includes a held request's text that this message joins. */
  includesHeld?: boolean
  /** Files can be attached here, so long text can go as one. */
  suggestAttach?: boolean
  className?: string
}) {
  const visible = length >= CHAR_COUNT_VISIBLE_FROM
  const over = isOverLimit(length)
  const hint = over ? hintText(length - MAX_MESSAGE_LENGTH, includesHeld, suggestAttach) : ""
  // Without the count, so it is said once on crossing the limit, not on each keystroke.
  const announcement = !over
    ? ""
    : includesHeld
      ? "Too long to send together with your held request."
      : "Message is too long to send."
  return (
    <>
      <span className="sr-only" aria-live="polite">
        {announcement}
      </span>
      <div id={id} className={cn(!visible && "hidden")}>
        {visible && (
          <p
            className={cn(
              "flex flex-wrap items-baseline justify-end gap-x-2 text-xs tabular-nums",
              over ? "text-destructive" : "text-muted-foreground",
              className,
            )}
            data-testid="chat-char-count"
            data-over-limit={over || undefined}
          >
            {over && (
              <span data-testid="chat-char-count-hint">
                {hint}
              </span>
            )}
            <span>
              {length.toLocaleString()} / {MAX_MESSAGE_LENGTH.toLocaleString()}
            </span>
          </p>
        )}
      </div>
    </>
  )
}
