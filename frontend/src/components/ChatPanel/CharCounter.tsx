import { cn } from "@/lib/utils"

import { CHAR_COUNT_VISIBLE_FROM, MAX_MESSAGE_LENGTH, isOverLimit } from "./messageLimit"

export function CharCounter({ length, className }: { length: number; className?: string }) {
  if (length < CHAR_COUNT_VISIBLE_FROM) return null
  const over = isOverLimit(length)
  const excess = length - MAX_MESSAGE_LENGTH
  return (
    <p
      className={cn(
        "flex flex-wrap items-baseline justify-end gap-x-2 text-xs tabular-nums",
        over ? "text-destructive" : "text-muted-foreground",
        className,
      )}
      data-testid="chat-char-count"
      data-over-limit={over || undefined}
    >
      <span aria-live="polite">
        {over && (
          <span data-testid="chat-char-count-hint">
            Too long to send. Shorten it by {excess.toLocaleString()}{" "}
            {excess === 1 ? "character" : "characters"}.
          </span>
        )}
      </span>
      <span>
        {length.toLocaleString()} / {MAX_MESSAGE_LENGTH.toLocaleString()}
      </span>
    </p>
  )
}
