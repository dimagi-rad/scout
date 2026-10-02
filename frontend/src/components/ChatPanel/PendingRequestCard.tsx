import { Fragment, useEffect, useRef, useState } from "react"
import { AlertCircle, Clock, Loader2, Pencil, X } from "lucide-react"

import type { PendingRequest } from "@/api/jobs"
import { pendingRequestText, type PendingPhase } from "@/api/pendingRequests"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import type { EditOutcome } from "./useHeldRequest"

interface PendingRequestCardProps {
  pending: PendingRequest
  phase: PendingPhase
  onSendNow?: () => void
  onDiscard?: () => void
  onEdit?: (text: string, baseVersion: number) => Promise<EditOutcome>
  /** An open edit the card can no longer save (the request is being sent, or the
   *  chat closed): its text goes where the user can still send it. */
  onAbandonEdit?: (text: string) => void
  onRemovePart?: (partId: string) => Promise<EditOutcome>
  /** While a turn is in flight, so a second send cannot overtake it. */
  actionsDisabled?: boolean
}

const HEADINGS: Record<PendingPhase, string> = {
  waiting: "Waiting for data",
  answering: "Answering…",
  unanswered: "Couldn't answer",
}

const ICONS: Record<PendingPhase, typeof Clock> = {
  waiting: Clock,
  answering: Loader2,
  unanswered: AlertCircle,
}

const CONFLICT_NOTICE =
  "Updated in another tab. Cancel and edit again to include the change; your text is still here."
const TOO_LATE_NOTICE = "Your request was already being sent."

/** The user's request held while their data loads, sent as one message once it can be answered. */
export function PendingRequestCard({
  pending,
  phase,
  onSendNow,
  onDiscard,
  onEdit,
  onRemovePart,
  onAbandonEdit,
  actionsDisabled = false,
}: PendingRequestCardProps) {
  const Icon = ICONS[phase]
  const editable = phase === "waiting"
  // Parts here when the card first showed came in with it; later ones slide in.
  const [initialPartIds] = useState(() => new Set(pending.parts.map((part) => part.id)))
  const [draft, setDraft] = useState<string | null>(null)
  // The version the open edit was made from; a save against any other is refused.
  const [draftBase, setDraftBase] = useState(pending.version)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const editing = draft !== null && editable

  const abandonRef = useRef(onAbandonEdit)
  abandonRef.current = onAbandonEdit
  const draftRef = useRef(draft)
  draftRef.current = draft
  // An edit open when the request started sending, handed on after this render.
  const abandonedRef = useRef<string | null>(null)
  if (draft !== null && !editable) {
    abandonedRef.current = draft
    setDraft(null)
    setNotice(`${TOO_LATE_NOTICE} Your edit is in the message box.`)
  }
  useEffect(() => {
    const abandoned = abandonedRef.current
    if (abandoned === null) return
    abandonedRef.current = null
    abandonRef.current?.(abandoned)
  }, [editable])
  // Closing the chat with an edit open keeps it too.
  useEffect(
    () => () => {
      const open = draftRef.current ?? abandonedRef.current
      if (open !== null) abandonRef.current?.(open)
    },
    [],
  )

  function settle(outcome: EditOutcome, keepDraft: boolean) {
    if (outcome === "saved") {
      setNotice(null)
      setDraft(null)
    } else if (outcome === "conflict") {
      // The card now shows the other tab's copy; the edit stays open to compare.
      setNotice(CONFLICT_NOTICE)
      if (!keepDraft) setDraft(null)
    } else if (outcome === "gone") {
      setNotice(keepDraft ? `${TOO_LATE_NOTICE} Your edit is in the message box.` : TOO_LATE_NOTICE)
      setDraft(null)
    } else {
      setNotice(outcome.failed)
    }
  }

  async function save() {
    if (draft === null || !onEdit) return
    setSaving(true)
    try {
      settle(await onEdit(draft, draftBase), true)
    } finally {
      setSaving(false)
    }
  }

  async function remove(partId: string) {
    if (!onRemovePart) return
    settle(await onRemovePart(partId), false)
  }

  return (
    <div className="flex w-full justify-end">
      <div
        className="max-w-[90%] rounded-lg border border-dashed border-primary/40 bg-primary/5 text-sm"
        data-testid="pending-request-card"
        data-phase={phase}
      >
        <div className="flex items-center justify-between gap-2 px-4 pt-2">
          <div
            className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground"
            data-testid="pending-request-status"
            aria-live="polite"
          >
            <Icon
              className={`h-3.5 w-3.5 ${phase === "answering" ? "animate-spin" : ""}`}
              aria-hidden="true"
            />
            {HEADINGS[phase]}
          </div>
          {editable && !editing && onEdit && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="h-6 px-2 text-xs"
              onClick={() => {
                setNotice(null)
                setDraftBase(pending.version)
                setDraft(pendingRequestText(pending))
              }}
              disabled={actionsDisabled}
              data-testid="pending-request-edit"
            >
              <Pencil className="h-3 w-3" aria-hidden="true" />
              Edit
            </Button>
          )}
        </div>
        {editing ? (
          <div className="flex flex-col gap-2 px-4 py-2">
            <Textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              aria-label="Edit your request"
              className="min-h-24"
              data-testid="pending-request-edit-text"
            />
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => {
                  setDraft(null)
                  setNotice(null)
                }}
                data-testid="pending-request-edit-cancel"
              >
                Cancel
              </Button>
              <Button
                type="button"
                size="sm"
                onClick={() => void save()}
                disabled={saving || !draft?.trim()}
                data-testid="pending-request-edit-save"
              >
                Save
              </Button>
            </div>
          </div>
        ) : (
          <div className="px-4 py-2">
            {pending.parts.map((part, index) => (
              <Fragment key={part.id}>
                {index > 0 && (
                  <hr className="my-2 border-t border-dashed border-primary/30" aria-hidden="true" />
                )}
                <div
                  className={`group flex items-start gap-2 ${
                    index > 0 && !initialPartIds.has(part.id)
                      ? "motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-1 motion-safe:duration-[250ms]"
                      : ""
                  }`}
                  data-testid={`pending-request-part-${part.id}`}
                >
                  <p className="flex-1 whitespace-pre-wrap">{part.text}</p>
                  {editable && index > 0 && onRemovePart && (
                    <button
                      type="button"
                      className="mt-0.5 rounded p-0.5 text-muted-foreground hover:bg-primary/10 hover:text-foreground disabled:opacity-50"
                      onClick={() => void remove(part.id)}
                      disabled={actionsDisabled}
                      aria-label="Remove this part"
                      data-testid={`pending-request-remove-${part.id}`}
                    >
                      <X className="h-3.5 w-3.5" aria-hidden="true" />
                    </button>
                  )}
                </div>
              </Fragment>
            ))}
          </div>
        )}
        {notice && (
          <p className="px-4 pb-1 text-xs text-destructive" role="status" data-testid="pending-request-notice">
            {notice}
          </p>
        )}
        {phase === "waiting" && !editing && (
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
