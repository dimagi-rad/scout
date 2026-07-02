import { useCallback, useEffect, useMemo, useState } from "react"
import type { ReactNode } from "react"
import { useParams } from "react-router-dom"
import {
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Code2,
  Columns3,
  Database,
  Link2,
  Loader2,
  MessageSquare,
  RefreshCw,
  RotateCcw,
  Save,
  Sigma,
  X,
} from "lucide-react"
import { ApiError } from "@/api/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import {
  applyCanvasOps,
  commitCanvas,
  fetchCanvas,
  fieldKind,
  fieldKindLabel,
  formatDiffKey,
  formatDiffValue,
  groupByDataset,
  pendingObjects,
  STATE_BADGES,
  type CanvasDatasetGroup,
  type CanvasObjectEntry,
  type CanvasOp,
  type CanvasProjection,
} from "./canvasApi"

const POLL_INTERVAL_MS = 15_000

interface ChatCanvasPanelProps {
  workspaceId: string
  threadId?: string
  className?: string
}

type LoadStatus = "idle" | "loading" | "loaded" | "error"

export function ChatCanvasPanel({ workspaceId, threadId, className }: ChatCanvasPanelProps) {
  const params = useParams<{ threadId?: string }>()
  const activeThreadId = threadId ?? params.threadId ?? null

  const [status, setStatus] = useState<LoadStatus>("idle")
  const [error, setError] = useState<string | null>(null)
  const [projection, setProjection] = useState<CanvasProjection | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  const loadCanvas = useCallback(
    async (silent = false) => {
      if (!activeThreadId) return
      if (!silent) setStatus("loading")
      try {
        const next = await fetchCanvas(workspaceId, activeThreadId)
        setProjection(next)
        setStatus("loaded")
        setError(null)
      } catch (loadError) {
        if (!silent) {
          setStatus("error")
          setError(loadError instanceof Error ? loadError.message : "Failed to load canvas")
        }
      }
    },
    [workspaceId, activeThreadId],
  )

  useEffect(() => {
    setProjection(null)
    setNotice(null)
    if (!activeThreadId) {
      setStatus("idle")
      return
    }
    void loadCanvas()
    // Agent edits land server-side mid-conversation; poll to keep the panel live.
    const timer = window.setInterval(() => void loadCanvas(true), POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [workspaceId, activeThreadId, loadCanvas])

  const runOps = useCallback(
    async (operations: CanvasOp[]): Promise<boolean> => {
      if (!activeThreadId) return false
      setBusy(true)
      setNotice(null)
      try {
        const next = await applyCanvasOps(workspaceId, activeThreadId, operations)
        setProjection(next)
        setError(null)
        return true
      } catch (applyError) {
        setError(applyError instanceof ApiError ? applyError.message : "Canvas change failed")
        return false
      } finally {
        setBusy(false)
      }
    },
    [workspaceId, activeThreadId],
  )

  const handleCommit = useCallback(async () => {
    if (!activeThreadId) return
    setBusy(true)
    setNotice(null)
    try {
      const report = await commitCanvas(workspaceId, activeThreadId)
      if (report.projection) setProjection(report.projection)
      if (report.blocked) {
        setError("Save blocked — fix the problems listed below first.")
      } else if (report.conflicts.length > 0) {
        setError("Save skipped: another change landed underneath your edits. Revert to refresh.")
      } else {
        setError(null)
        const saved = report.committed.length
        const cube = report.cube_schema
        setNotice(
          cube && !cube.ok
            ? `Saved ${saved} change(s), but the query model rebuild failed: ${cube.error ?? "unknown error"}`
            : `Saved ${saved} change(s) to the semantic model.`,
        )
      }
    } catch (commitError) {
      setError(commitError instanceof Error ? commitError.message : "Canvas save failed")
    } finally {
      setBusy(false)
    }
  }, [workspaceId, activeThreadId])

  const pendingCount = useMemo(() => pendingObjects(projection).length, [projection])
  const groups = useMemo(() => groupByDataset(projection), [projection])
  const diagnostics = projection?.diagnostics ?? []

  if (!activeThreadId) {
    return (
      <PanelShell className={className}>
        <PanelState
          icon={<MessageSquare className="h-5 w-5" />}
          title="No conversation yet"
          body="Start a conversation to draft changes to this workspace's datasets."
        />
      </PanelShell>
    )
  }

  return (
    <PanelShell className={className}>
      <div className="flex shrink-0 items-center justify-between gap-3 border-b px-3 py-2">
        <p className="truncate text-xs text-muted-foreground" data-testid="canvas-pending-count">
          {pendingCount > 0
            ? `${pendingCount} pending change${pendingCount === 1 ? "" : "s"}`
            : "No pending changes"}
        </p>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon-xs"
            onClick={() => void loadCanvas()}
            disabled={status === "loading"}
            aria-label="Refresh canvas"
            data-testid="canvas-refresh-button"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", status === "loading" && "animate-spin")} />
          </Button>
          <Button
            size="xs"
            onClick={() => void handleCommit()}
            disabled={busy || !projection?.can_commit}
            data-testid="canvas-commit-button"
          >
            {busy ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Save className="h-3.5 w-3.5" />
            )}
            Save all
          </Button>
        </div>
      </div>

      {status === "loading" && !projection ? (
        <PanelState icon={<Loader2 className="h-5 w-5 animate-spin" />} title="Loading canvas" />
      ) : status === "error" && !projection ? (
        <PanelState
          icon={<CircleAlert className="h-5 w-5" />}
          title="Canvas unavailable"
          body={error ?? "Could not load the canvas."}
          action={
            <Button size="sm" onClick={() => void loadCanvas()}>
              Try Again
            </Button>
          }
        />
      ) : groups.length === 0 ? (
        <PanelState
          icon={<Database className="h-5 w-5" />}
          title="Nothing on the canvas"
          body="Ask the agent to edit datasets, add measures, link datasets, or build a SQL dataset — drafts will appear here for review."
        />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="space-y-3 p-3">
            {error && (
              <div
                className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive"
                data-testid="canvas-error"
              >
                {error}
              </div>
            )}
            {notice && (
              <div className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-900">
                {notice}
              </div>
            )}

            {diagnostics.length > 0 && <ProblemsPanel diagnostics={diagnostics} />}

            <div className="space-y-2">
              {groups.map((group) => (
                <DatasetGroupCard key={group.name} group={group} busy={busy} onOps={runOps} />
              ))}
            </div>
          </div>
        </div>
      )}
    </PanelShell>
  )
}

function PanelShell({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div className={cn("flex h-full min-h-0 flex-col bg-background", className)}>{children}</div>
  )
}

function ProblemsPanel({ diagnostics }: { diagnostics: CanvasProjection["diagnostics"] }) {
  return (
    <section
      className="rounded-md border border-destructive/30"
      data-testid="canvas-problems-panel"
    >
      <div className="flex items-center gap-2 border-b border-destructive/20 bg-destructive/5 px-3 py-2">
        <CircleAlert className="h-4 w-4 text-destructive" />
        <h3 className="text-xs font-semibold uppercase tracking-wide text-destructive">
          Problems
        </h3>
        <Badge variant="secondary">{diagnostics.length}</Badge>
      </div>
      <div className="divide-y">
        {diagnostics.map((diagnostic, index) => (
          <div key={`${diagnostic.object_uuid}-${diagnostic.code}-${index}`} className="px-3 py-2">
            <div className="text-xs font-medium">
              {diagnostic.object}
              {diagnostic.path ? `/${diagnostic.path}` : ""}
            </div>
            <p className="mt-0.5 text-xs text-muted-foreground">{diagnostic.message}</p>
          </div>
        ))}
      </div>
    </section>
  )
}

function DatasetGroupCard({
  group,
  busy,
  onOps,
}: {
  group: CanvasDatasetGroup
  busy: boolean
  onOps: (ops: CanvasOp[]) => Promise<boolean>
}) {
  const [expanded, setExpanded] = useState(group.pendingCount > 0)
  const badge = STATE_BADGES[group.state]
  const dataset = group.dataset
  const hasDetail =
    group.fields.length > 0
    || group.relationships.length > 0
    || (dataset != null && Object.keys(dataset.diff).length > 0)
  const measures = group.fields.filter((entry) => fieldKind(entry) === "measure")
  const dimensions = group.fields.filter((entry) => fieldKind(entry) !== "measure")

  return (
    <section className="rounded-md border" data-testid={`canvas-dataset-${group.name}`}>
      <div className="flex items-start justify-between gap-2 px-3 py-2">
        <button
          type="button"
          className="flex min-w-0 flex-1 items-start gap-2 text-left"
          onClick={() => setExpanded((current) => !current)}
        >
          {hasDetail ? (
            expanded ? (
              <ChevronDown className="mt-1 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            ) : (
              <ChevronRight className="mt-1 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            )
          ) : (
            <span className="w-3.5 shrink-0" />
          )}
          <span className="min-w-0">
            <span className="flex min-w-0 flex-wrap items-center gap-2">
              {group.isCte ? (
                <Code2 className="h-4 w-4 shrink-0 text-muted-foreground" />
              ) : (
                <Database className="h-4 w-4 shrink-0 text-muted-foreground" />
              )}
              <span className="truncate text-sm font-medium">{group.label}</span>
              <Badge className={cn("border", badge.className)} variant="outline">
                {badge.label}
              </Badge>
            </span>
            <span className="mt-0.5 block truncate text-xs text-muted-foreground">
              {group.summary}
            </span>
          </span>
        </button>
        {dataset && (
          <EntryActions entry={dataset} busy={busy} onOps={onOps} showRevert={dataset.state !== "unchanged"} />
        )}
      </div>

      {expanded && hasDetail && (
        <div className="border-t bg-muted/30">
          {dataset && Object.keys(dataset.diff).length > 0 && (
            <DiffLines entry={dataset} className="border-b px-3 py-2" />
          )}
          {dataset?.object_type === "custom_dataset" && <CteDraftDetail entry={dataset} />}
          {measures.length > 0 && (
            <FieldSection
              title="Measures"
              entries={measures}
              icon={<Sigma className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
              busy={busy}
              onOps={onOps}
            />
          )}
          {dimensions.length > 0 && (
            <FieldSection
              title="Dimensions"
              entries={dimensions}
              icon={<Columns3 className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
              busy={busy}
              onOps={onOps}
            />
          )}
          {group.relationships.map((entry) => (
            <ChildRow
              key={entry.object_uuid}
              entry={entry}
              icon={<Link2 className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
              title={`→ ${relationshipTarget(entry)}`}
              detail={relationshipDetail(entry)}
              busy={busy}
              onOps={onOps}
            />
          ))}
        </div>
      )}
    </section>
  )
}

function FieldSection({
  title,
  entries,
  icon,
  busy,
  onOps,
}: {
  title: string
  entries: CanvasObjectEntry[]
  icon: ReactNode
  busy: boolean
  onOps: (ops: CanvasOp[]) => Promise<boolean>
}) {
  return (
    <div className="border-b last:border-b-0">
      <div className="px-3 pt-2 pl-8 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </div>
      {entries.map((entry) => (
        <ChildRow
          key={entry.object_uuid}
          entry={entry}
          icon={icon}
          title={entry.label || entry.name}
          detail={fieldDetail(entry)}
          busy={busy}
          onOps={onOps}
        />
      ))}
    </div>
  )
}

function ChildRow({
  entry,
  icon,
  title,
  detail,
  busy,
  onOps,
}: {
  entry: CanvasObjectEntry
  icon: ReactNode
  title: string
  detail: string
  busy: boolean
  onOps: (ops: CanvasOp[]) => Promise<boolean>
}) {
  const badge = STATE_BADGES[entry.state]
  const showDiff = entry.state === "edited" && Object.keys(entry.diff).length > 0
  return (
    <div
      className="border-b px-3 py-2 last:border-b-0"
      data-testid={`canvas-child-${entry.name}`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 flex-1 items-start gap-2 pl-5">
          <span className="mt-0.5">{icon}</span>
          <span className="min-w-0">
            <span className="flex min-w-0 flex-wrap items-center gap-2">
              <code className="truncate rounded bg-muted px-1.5 py-0.5 text-xs">{title}</code>
              {entry.state !== "unchanged" && (
                <Badge className={cn("border", badge.className)} variant="outline">
                  {badge.label}
                </Badge>
              )}
            </span>
            {detail && (
              <span className="mt-0.5 block truncate text-xs text-muted-foreground">{detail}</span>
            )}
          </span>
        </div>
        <EntryActions entry={entry} busy={busy} onOps={onOps} showRevert={entry.state !== "unchanged"} />
      </div>
      {showDiff && <DiffLines entry={entry} className="mt-1 pl-10" />}
    </div>
  )
}

function EntryActions({
  entry,
  busy,
  onOps,
  showRevert,
}: {
  entry: CanvasObjectEntry
  busy: boolean
  onOps: (ops: CanvasOp[]) => Promise<boolean>
  showRevert: boolean
}) {
  const objectRef = `${entry.object_type}/${entry.object_uuid}`
  return (
    <div className="flex shrink-0 items-center gap-1">
      {showRevert && (
        <Button
          variant="ghost"
          size="icon-xs"
          onClick={() => void onOps([{ op: "revert_object", object: objectRef }])}
          disabled={busy}
          aria-label={`Revert ${entry.name}`}
          data-testid={`canvas-revert-${entry.name}`}
        >
          <RotateCcw className="h-3.5 w-3.5" />
        </Button>
      )}
      <Button
        variant="ghost"
        size="icon-xs"
        onClick={() => void onOps([{ op: "remove_from_canvas", object: objectRef }])}
        disabled={busy}
        aria-label={`Remove ${entry.name} from canvas`}
        data-testid={`canvas-remove-${entry.name}`}
      >
        <X className="h-3.5 w-3.5" />
      </Button>
    </div>
  )
}

function DiffLines({ entry, className }: { entry: CanvasObjectEntry; className?: string }) {
  const showFrom = entry.change_type !== "create"
  return (
    <div className={cn("space-y-1", className)}>
      {Object.entries(entry.diff).map(([key, delta]) => (
        <div key={key} className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-2 text-xs">
          <span className="truncate font-medium">{formatDiffKey(key)}</span>
          <span className="min-w-0">
            {showFrom && (
              <span className="text-muted-foreground line-through">
                {formatDiffValue(delta.from)}
              </span>
            )}{" "}
            <span className="font-medium text-foreground">{formatDiffValue(delta.to)}</span>
          </span>
        </div>
      ))}
    </div>
  )
}

function CteDraftDetail({ entry }: { entry: CanvasObjectEntry }) {
  const sql = typeof entry.fields.definition_sql === "string" ? entry.fields.definition_sql : ""
  const columns = Array.isArray(entry.fields.columns) ? entry.fields.columns : []
  return (
    <div className="border-b px-3 py-2">
      {sql && (
        <pre className="max-h-36 overflow-y-auto whitespace-pre-wrap rounded bg-muted px-2 py-1.5 font-mono text-[11px] leading-4 text-muted-foreground">
          {sql}
        </pre>
      )}
      {columns.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {columns.map((column) => {
            const name = typeof column === "object" && column !== null ? (column as { name?: string }).name : String(column)
            return (
              <Badge key={String(name)} variant="secondary" className="font-mono text-[10px]">
                {String(name)}
              </Badge>
            )
          })}
        </div>
      )}
    </div>
  )
}

function fieldDetail(entry: CanvasObjectEntry): string {
  if (entry.state === "edited") {
    const changed = Object.keys(entry.diff).sort().map(formatDiffKey).join(", ")
    return `${fieldKindLabel(entry)} · ${changed} changed`
  }
  const fields = entry.fields
  const fieldType = typeof fields.field_type === "string" ? fields.field_type : ""
  const measureType = typeof fields.measure_type === "string" ? fields.measure_type : ""
  const expression = typeof fields.expression === "string" ? fields.expression : ""
  if (fieldType === "measure") {
    return measureType === "count" ? "count of rows" : `${measureType} of ${expression}`
  }
  if (fieldType) {
    return `${fieldType.replace("_", " ")} on ${expression}`
  }
  return ""
}

function relationshipTarget(entry: CanvasObjectEntry): string {
  const draft = entry.fields.to_dataset
  if (typeof draft === "string" && draft) return draft
  const base = entry.base.to_dataset
  if (typeof base === "string" && base) return base
  return entry.name
}

function relationshipDetail(entry: CanvasObjectEntry): string {
  const source = entry.change_type === "create" ? entry.fields : entry.base
  const type = typeof source.relationship_type === "string" ? source.relationship_type : ""
  const fromField = typeof source.from_field === "string" ? source.from_field : ""
  return [type, fromField && `via ${fromField}`].filter(Boolean).join(" · ")
}

function PanelState({
  icon,
  title,
  body,
  action,
}: {
  icon: ReactNode
  title: string
  body?: string
  action?: ReactNode
}) {
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center p-6 text-center text-muted-foreground">
      <div>
        <div className="mb-3 flex justify-center">{icon}</div>
        <h3 className="text-sm font-medium text-foreground">{title}</h3>
        {body && <p className="mt-1 max-w-xs text-sm">{body}</p>}
        {action && <div className="mt-3">{action}</div>}
      </div>
    </div>
  )
}
