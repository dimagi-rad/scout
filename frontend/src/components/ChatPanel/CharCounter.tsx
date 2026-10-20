import { cn } from "@/lib/utils"

import { CHAR_COUNT_VISIBLE_FROM, MAX_MESSAGE_LENGTH, isOverLimit } from "./messageLimit"

function hintText(excess: number, includesHeld: boolean): string {
  const amount = `${excess.toLocaleString()} ${excess === 1 ? "character" : "characters"}`
  return includesHeld
    ? `Together with your waiting request this is ${amount} too long. Shorten it, or discard the request.`
    : `Too long to send. Shorten it by ${amount}.`
}

/**
 * The field points at this with aria-describedby, so it stays mounted. Only the
 * over-limit hint is announced, from a region that is always in the accessibility
 * tree: one added together with its text is often not read, and announcing the
 * running total would speak on every keystroke past the threshold.
 */
export function CharCounter({
  id,
  length,
  includesHeld = false,
  className,
}: {
  id: string
  length: number
  /** The count includes a held request's text that this message joins. */
  includesHeld?: boolean
  className?: string
}) {
  const visible = length >= CHAR_COUNT_VISIBLE_FROM
  const over = isOverLimit(length)
  const hint = over ? hintText(length - MAX_MESSAGE_LENGTH, includesHeld) : ""
  return (
    <>
      <span className="sr-only" aria-live="polite">
        {hint}
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
