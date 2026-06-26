import { useEffect } from "react"

import { ApiError } from "@/api/client"
import { Button } from "@/components/ui/button"

function isStaleThreadError(error: Error | undefined): boolean {
  if (!error) return false
  if (error instanceof ApiError && error.status === 404) return true
  return error.message.includes("Thread not found")
}

interface ChatErrorNoticeProps {
  error: Error
  onStartNewThread: () => void
}

/**
 * Friendly chat error. Never renders a raw response body. The stale-thread case
 * gets a recovery button; everything else gets a generic message.
 */
export function ChatErrorNotice({ error, onStartNewThread }: ChatErrorNoticeProps) {
  const stale = isStaleThreadError(error)
  useEffect(() => {
    console.error("[Scout] Chat error:", error)
  }, [error])

  return (
    <div
      className="text-sm text-destructive bg-destructive/10 rounded-lg px-4 py-3 space-y-2"
      data-testid="chat-error"
    >
      <p>
        {stale
          ? "This conversation is no longer available."
          : "Something went wrong. Please try again."}
      </p>
      {stale && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onStartNewThread}
          data-testid="chat-error-new-thread"
        >
          Start new chat
        </Button>
      )}
    </div>
  )
}

interface ChatOverloadNoticeProps {
  onRetry: () => void
}

export function ChatOverloadNotice({ onRetry }: ChatOverloadNoticeProps) {
  return (
    <div
      className="text-sm text-muted-foreground bg-muted rounded-lg px-4 py-3 space-y-2"
      data-testid="chat-overload-notice"
    >
      <p>The assistant is busy right now. Please try again in a moment.</p>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={onRetry}
        data-testid="chat-overload-retry"
      >
        Retry
      </Button>
    </div>
  )
}

export function ChatThinkingIndicator() {
  return (
    <div className="flex items-start gap-3 py-2" data-testid="thinking-indicator">
      <div className="flex items-center gap-1.5 rounded-lg bg-muted px-4 py-3">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className="block h-2 w-2 rounded-full bg-muted-foreground/60"
            style={{
              animation: "thinking-dot 1.4s ease-in-out infinite",
              animationDelay: `${i * 0.2}s`,
            }}
          />
        ))}
      </div>
      <style>{`
        @keyframes thinking-dot {
          0%, 80%, 100% { opacity: 0.3; transform: scale(0.8); }
          40% { opacity: 1; transform: scale(1); }
        }
      `}</style>
    </div>
  )
}
