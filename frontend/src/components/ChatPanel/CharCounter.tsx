import { cn } from "@/lib/utils"

import { CHAR_COUNT_VISIBLE_FROM, MAX_MESSAGE_LENGTH, isOverLimit } from "./messageLimit"

/**
 * Always mounted, empty below the threshold: a live region added together with its
 * text is often not announced, and the field points at it with aria-describedby.
 */
export function CharCounter({
  id,
  length,
  includesHeld = false,
  className,
}: {
  id: string
  length: number
  /** The count includes a held request's text, which the composer can't shorten. */
  includesHeld?: boolean
  className?: string
}) {
  const visible = length >= CHAR_COUNT_VISIBLE_FROM
  const over = isOverLimit(length)
  const excess = length - MAX_MESSAGE_LENGTH
  return (
    <div id={id} aria-live="polite" className={cn(!visible && "hidden")}>
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
              {includesHeld ? (
                <>Together with your waiting request this is too long. Edit or send that request first.</>
              ) : (
                <>
                  Too long to send. Shorten it by {excess.toLocaleString()}{" "}
                  {excess === 1 ? "character" : "characters"}.
                </>
              )}
            </span>
          )}
          <span>
            {length.toLocaleString()} / {MAX_MESSAGE_LENGTH.toLocaleString()}
          </span>
        </p>
      )}
    </div>
  )
}
