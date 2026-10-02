import { Fragment } from "react"
import { Clock, Loader2, AlertCircle } from "lucide-react"

import type { PendingRequest } from "@/api/jobs"
import type { PendingPhase } from "@/api/pendingRequests"
import { Button } from "@/components/ui/button"

interface PendingRequestCardProps {
  pending: PendingRequest
  phase: PendingPhase
  onSendNow?: () => void
  onDiscard?: () => void
  /** While a turn is in flight, so a second send cannot overtake it. */
  actionsDisabled?: boolean
}

const HEADINGS: Record<PendingPhase, string> = {
  waiting: "Waiting for data",
  answering: "Answering…",
  unanswered: "Couldn't answer",
}

/** The user's request held while their data loads, sent as one message once it can be answered. */
export function PendingRequestCard({
  pending,
  phase,
  onSendNow,
  onDiscard,
  actionsDisabled = false,
}: PendingRequestCardProps) {
  const Icon = phase === "answering" ? Loader2 : phase === "waiting" ? Clock : AlertCircle
  return (
    <div className="flex w-full justify-end">
      <div
        className="max-w-[90%] rounded-lg border border-dashed border-primary/40 bg-primary/5 text-sm"
        data-testid="pending-request-card"
        data-phase={phase}
      >
        <div
          className="flex items-center gap-1.5 px-4 pt-2 text-xs font-medium text-muted-foreground"
          data-testid="pending-request-status"
          aria-live="polite"
        >
          <Icon
            className={`h-3.5 w-3.5 ${phase === "answering" ? "animate-spin" : ""}`}
            aria-hidden="true"
          />
          {HEADINGS[phase]}
        </div>
        <div className="px-4 py-2">
          {pending.parts.map((part, index) => (
            <Fragment key={part.id}>
              {index > 0 && (
                <hr className="my-2 border-t border-dashed border-primary/30" aria-hidden="true" />
              )}
              <p className="whitespace-pre-wrap" data-testid={`pending-request-part-${part.id}`}>
                {part.text}
              </p>
            </Fragment>
          ))}
        </div>
        {phase === "waiting" && (
          <div className="flex items-center justify-between gap-2 px-4 pb-2">
            <p className="text-xs text-muted-foreground">
              Sent as one message when your data is ready
            </p>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              onClick={onDiscard}
              disabled={actionsDisabled}
              data-testid="pending-request-discard-waiting"
            >
              Discard
            </Button>
          </div>
        )}
        {phase === "unanswered" && (
          <div className="flex items-center gap-2 px-4 pb-3">
            <Button
              type="button"
              size="sm"
              onClick={onSendNow}
              disabled={actionsDisabled}
              data-testid="pending-request-send-now"
            >
              Send now
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={onDiscard}
              disabled={actionsDisabled}
              data-testid="pending-request-discard"
            >
              Discard
            </Button>
          </div>
        )}
      </div>
    </div>
  )
}
