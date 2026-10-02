import { useEffect } from "react"
import { Link } from "react-router-dom"

import { BUSY_MESSAGE } from "@/api/busy"
import { Button } from "@/components/ui/button"

import { ACCESS_RETRY_MESSAGE, GENERIC_CHAT_ERROR_MESSAGE, classifyChatError } from "./chatErrors"

interface ChatErrorNoticeProps {
  error: Error
  onStartNewThread: () => void
  onRetry: () => void
  /** "/embed" inside the embedded app, whose routes all live under it. */
  pathPrefix?: string
}

/**
 * Friendly chat error. Never renders an unrecognised response body: a stale
 * thread offers a new chat, an access denial its reason-specific remedy, a
 * request that cannot succeed as sent the backend's explanation, and anything
 * else a generic message with Retry and a new chat.
 */
export function ChatErrorNotice({
  error,
  onStartNewThread,
  onRetry,
  pathPrefix = "",
}: ChatErrorNoticeProps) {
  const classified = classifyChatError(error)
  useEffect(() => {
    console.error("[Scout] Chat error:", error)
  }, [error])

  const retryButton = (
    <Button
      type="button"
      variant="outline"
      size="sm"
      onClick={onRetry}
      data-testid="chat-error-retry"
    >
      Retry
    </Button>
  )

  const newThreadButton = (
    <Button
      type="button"
      variant="outline"
      size="sm"
      onClick={onStartNewThread}
      data-testid="chat-error-new-thread"
    >
      Start new chat
    </Button>
  )

  return (
    <div
      className="text-sm text-destructive bg-destructive/10 rounded-lg px-4 py-3 space-y-2"
      data-testid="chat-error"
      data-error-kind={classified.kind}
    >
      {classified.kind === "stale" && (
        <>
          <p data-testid="chat-error-message">This conversation is no longer available.</p>
          {newThreadButton}
        </>
      )}
      {classified.kind === "access-retry" && (
        <>
          <p data-testid="chat-error-message">{ACCESS_RETRY_MESSAGE}</p>
          {retryButton}
        </>
      )}
      {classified.kind === "access-reconnect" && (
        <>
          <p data-testid="chat-error-message">{classified.message}</p>
          <Button asChild variant="outline" size="sm">
            <Link
              to={`${pathPrefix}${classified.recoveryPath}`}
              data-testid="chat-error-recovery-link"
            >
              Connected Accounts
            </Link>
          </Button>
        </>
      )}
      {classified.kind === "final" && (
        <p data-testid="chat-error-message">{classified.message}</p>
      )}
      {classified.kind === "generic" && (
        <>
          <p data-testid="chat-error-message">{GENERIC_CHAT_ERROR_MESSAGE}</p>
          <div className="flex gap-2">
            {retryButton}
            {newThreadButton}
          </div>
        </>
      )}
    </div>
  )
}

interface RetryNoticeProps {
  onRetry: () => void
}

function RetryNotice({
  message,
  testId,
  onRetry,
}: RetryNoticeProps & { message: string; testId: string }) {
  return (
    <div
      className="text-sm text-muted-foreground bg-muted rounded-lg px-4 py-3 space-y-2"
      role="status"
      data-testid={`${testId}-notice`}
    >
      <p>{message}</p>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={onRetry}
        data-testid={`${testId}-retry`}
      >
        Retry
      </Button>
    </div>
  )
}

export function ChatOverloadNotice({ onRetry }: RetryNoticeProps) {
  return (
    <RetryNotice
      message="The assistant is busy right now. Please try again in a moment."
      testId="chat-overload"
      onRetry={onRetry}
    />
  )
}

/** Scout hit a connection limit and the automatic retries are spent. */
export function ChatBusyNotice({ onRetry }: RetryNoticeProps) {
  return <RetryNotice message={BUSY_MESSAGE} testId="chat-busy" onRetry={onRetry} />
}

export function ChatStoppedNotice() {
  return (
    <p
      className="text-sm text-muted-foreground italic"
      data-testid="chat-stopped-notice"
      role="status"
    >
      Response stopped by user.
    </p>
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
