import { useCallback, useState } from "react"
import { History, RefreshCw, Undo2 } from "lucide-react"
import { api, ApiError, asRecord } from "@/api/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { cn } from "@/lib/utils"

export interface DataModelRevision {
  id: string
  source: "canvas_commit" | "undo"
  summary: string
  created_at: string
  created_by: { id: string; name: string } | null
  reverts_id: string | null
  undone: boolean
}

interface RevisionListResponse {
  revisions: DataModelRevision[]
  can_undo: boolean
}

function conflictDetails(error: unknown): string[] {
  if (!(error instanceof ApiError)) return []
  const conflicts = asRecord(error.body)?.conflicts
  if (!Array.isArray(conflicts)) return []
  return conflicts
    .map((conflict) => {
      const record = asRecord(conflict)
      const object = typeof record?.object === "string" ? record.object : ""
      const message = typeof record?.message === "string" ? record.message : ""
      return [object, message].filter(Boolean).join(": ")
    })
    .filter(Boolean)
}

export function DataModelHistory({
  workspaceId,
  onChanged,
  testIdPrefix = "data-model-history",
  className,
}: {
  workspaceId: string | null
  onChanged: () => void | Promise<void>
  testIdPrefix?: string
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [revisions, setRevisions] = useState<DataModelRevision[]>([])
  const [canUndo, setCanUndo] = useState(false)
  const [undoingId, setUndoingId] = useState<string | null>(null)
  const [error, setError] = useState<{ message: string; details: string[] } | null>(null)
  const base = workspaceId ? `/api/workspaces/${workspaceId}/data-model/revisions/` : null

  const load = useCallback(async () => {
    if (!base) return
    setLoading(true)
    try {
      const response = await api.get<RevisionListResponse>(base)
      setRevisions(response.revisions)
      setCanUndo(response.can_undo)
    } catch (err) {
      setError({ message: err instanceof Error ? err.message : "Could not load history.", details: [] })
    } finally {
      setLoading(false)
    }
  }, [base])

  const handleOpenChange = (next: boolean) => {
    setOpen(next)
    if (next) {
      setError(null)
      void load()
    }
  }

  const undo = async (revision: DataModelRevision) => {
    if (!base) return
    setUndoingId(revision.id)
    setError(null)
    try {
      await api.post(`${base}${revision.id}/undo/`)
      await Promise.all([load(), onChanged()])
    } catch (err) {
      setError({
        message: err instanceof Error ? err.message : "Could not undo this change.",
        details: conflictDetails(err),
      })
    } finally {
      setUndoingId(null)
    }
  }

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className={cn("h-8 w-8", className)}
          aria-label="Data model history"
          title="Data model history"
          disabled={!workspaceId}
          data-testid={`${testIdPrefix}-btn`}
        >
          <History className="h-4 w-4" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-96 p-0" data-testid={`${testIdPrefix}-panel`}>
        <div className="border-b px-4 py-3">
          <h2 className="text-sm font-semibold">Data model history</h2>
          <p className="text-xs text-muted-foreground">
            Every saved dataset change, newest first.
            {canUndo ? " Undo restores what a change replaced." : ""}
          </p>
        </div>
        {error && (
          <div
            className="border-b bg-destructive/10 px-4 py-2 text-xs text-destructive"
            role="alert"
            data-testid={`${testIdPrefix}-error`}
          >
            <p>{error.message}</p>
            {error.details.length > 0 && (
              <ul className="mt-1 list-disc pl-4">
                {error.details.map((detail) => (
                  <li key={detail}>{detail}</li>
                ))}
              </ul>
            )}
          </div>
        )}
        <div className="max-h-96 overflow-y-auto">
          {loading && revisions.length === 0 ? (
            <div className="flex items-center gap-2 px-4 py-6 text-sm text-muted-foreground">
              <RefreshCw className="h-4 w-4 animate-spin" /> Loading history
            </div>
          ) : revisions.length === 0 ? (
            <p className="px-4 py-6 text-sm text-muted-foreground">No saved changes yet.</p>
          ) : (
            <ul className="divide-y">
              {revisions.map((revision) => (
                <li
                  key={revision.id}
                  className="flex items-start gap-3 px-4 py-3"
                  data-testid={`${testIdPrefix}-item-${revision.id}`}
                >
                  <div className="min-w-0 flex-1">
                    <p className="break-words text-sm">{revision.summary}</p>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      {new Date(revision.created_at).toLocaleString()}
                      {revision.created_by ? ` · ${revision.created_by.name}` : ""}
                    </p>
                  </div>
                  {revision.undone ? (
                    <Badge variant="outline" className="shrink-0">
                      Undone
                    </Badge>
                  ) : canUndo ? (
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-7 shrink-0 gap-1 px-2 text-xs"
                      onClick={() => void undo(revision)}
                      disabled={undoingId !== null}
                      data-testid={`${testIdPrefix}-undo-${revision.id}`}
                    >
                      <Undo2 className={cn("h-3.5 w-3.5", undoingId === revision.id && "animate-pulse")} />
                      Undo
                    </Button>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </div>
      </PopoverContent>
    </Popover>
  )
}
