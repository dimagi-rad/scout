/**
 * Client + types for the thread-bound semantic canvas changeset API.
 *
 * The server is the source of truth: the panel renders projections grouped by
 * dataset (institute-style: datasets first, their edits nested underneath).
 * All edits flow through the agent's Canvas Manager; the panel only reviews,
 * reverts, removes, and commits.
 */

import { api } from "@/api/client"

export type CanvasState = "new" | "edited" | "unchanged" | "deleted" | "conflict"

export type CanvasObjectType = "dataset" | "field" | "relationship" | "custom_dataset"
export type CanvasFieldKind = "measure" | "time_dimension" | "dimension" | "field"

export interface CanvasDiagnostic {
  code: string
  severity: "error" | "warning"
  object: string
  object_uuid: string
  path: string
  message: string
}

export interface CanvasDiffEntry {
  from: unknown
  to: unknown
}

export interface CanvasObjectEntry {
  key: string
  object_type: CanvasObjectType
  object_uuid: string
  change_type: "create" | "update" | "delete"
  name: string
  label: string
  dataset: string
  state: CanvasState
  summary: string
  diff: Record<string, CanvasDiffEntry>
  fields: Record<string, unknown>
  base: Record<string, unknown>
}

export interface CanvasProjection {
  canvas: {
    id: string
    thread_id: string
    status: string
    committed_at: string | null
    updated_at: string
  }
  objects: CanvasObjectEntry[]
  diagnostics: CanvasDiagnostic[]
  pending_count: number
  can_commit: boolean
}

export interface CanvasCommitReport {
  committed: Array<Record<string, unknown>>
  blocked: boolean
  blocking_diagnostics: CanvasDiagnostic[]
  conflicts: Array<Record<string, unknown>>
  cube_schema?: { ok: boolean; error?: string }
  projection?: CanvasProjection
}

export type CanvasOp = Record<string, unknown>

function canvasUrl(workspaceId: string, threadId: string, suffix = ""): string {
  return `/api/workspaces/${workspaceId}/threads/${threadId}/canvas/${suffix}`
}

export function fetchCanvas(workspaceId: string, threadId: string): Promise<CanvasProjection> {
  return api.get<CanvasProjection>(canvasUrl(workspaceId, threadId))
}

export function applyCanvasOps(
  workspaceId: string,
  threadId: string,
  operations: CanvasOp[],
): Promise<CanvasProjection> {
  return api.post<CanvasProjection>(canvasUrl(workspaceId, threadId, "apply/"), { operations })
}

export function commitCanvas(
  workspaceId: string,
  threadId: string,
): Promise<CanvasCommitReport> {
  return api.post<CanvasCommitReport>(canvasUrl(workspaceId, threadId, "commit/"), {})
}

export const STATE_BADGES: Record<CanvasState, { label: string; className: string }> = {
  new: { label: "New", className: "border-emerald-200 bg-emerald-50 text-emerald-800" },
  edited: { label: "Edited", className: "border-amber-200 bg-amber-50 text-amber-900" },
  unchanged: { label: "Saved", className: "border-border bg-muted text-muted-foreground" },
  deleted: { label: "Will be removed", className: "border-red-200 bg-red-50 text-red-800" },
  conflict: {
    label: "Conflict",
    className: "border-destructive/30 bg-destructive/10 text-destructive",
  },
}

export function formatDiffValue(value: unknown, limit = 160): string {
  if (value === null || value === undefined || value === "") return "(empty)"
  const text = String(value).replace(/\s+/g, " ").trim()
  return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`
}

const DIFF_LABELS: Record<string, string> = {
  currency: "Currency",
  data_type: "Data type",
  description: "Description",
  expression: "Expression",
  field_type: "Field type",
  format: "Value format",
  label: "Label",
  measure_type: "Measure type",
  name: "Name",
  primary_key: "Primary key",
  relationship_type: "Relationship type",
}

export function formatDiffKey(key: string): string {
  return DIFF_LABELS[key] ?? key.replace(/_/g, " ")
}

function stringField(source: Record<string, unknown>, key: string): string {
  const value = source[key]
  return typeof value === "string" ? value : ""
}

export function fieldKind(entry: CanvasObjectEntry): CanvasFieldKind {
  const fieldType = stringField(entry.fields, "field_type") || stringField(entry.base, "field_type")
  if (fieldType === "measure") return "measure"
  if (fieldType === "time_dimension") return "time_dimension"
  if (fieldType === "dimension") return "dimension"
  return "field"
}

export function fieldKindLabel(entry: CanvasObjectEntry): string {
  const kind = fieldKind(entry)
  if (kind === "time_dimension") return "time dimension"
  return kind
}

/** Objects with anything pending — drives the count badge + commit gating. */
export function pendingObjects(projection: CanvasProjection | null): CanvasObjectEntry[] {
  return (projection?.objects ?? []).filter((entry) => entry.state !== "unchanged")
}

/** Institute-style grouping: one row per dataset, its edits nested under it. */
export interface CanvasDatasetGroup {
  name: string
  label: string
  isCte: boolean
  dataset: CanvasObjectEntry | null
  fields: CanvasObjectEntry[]
  relationships: CanvasObjectEntry[]
  state: CanvasState
  pendingCount: number
  summary: string
}

const GROUP_STATE_ORDER: Record<CanvasState, number> = {
  conflict: 0,
  new: 1,
  edited: 2,
  deleted: 3,
  unchanged: 4,
}

function relationshipDatasetName(entry: CanvasObjectEntry): string {
  const fromDraft = entry.fields.from_dataset
  if (typeof fromDraft === "string" && fromDraft) return fromDraft
  const fromBase = entry.base.from_dataset
  if (typeof fromBase === "string" && fromBase) return fromBase
  return entry.dataset || entry.name
}

export function groupByDataset(projection: CanvasProjection | null): CanvasDatasetGroup[] {
  const groups = new Map<string, CanvasDatasetGroup>()

  const groupFor = (name: string): CanvasDatasetGroup => {
    let group = groups.get(name)
    if (!group) {
      group = {
        name,
        label: name,
        isCte: false,
        dataset: null,
        fields: [],
        relationships: [],
        state: "unchanged",
        pendingCount: 0,
        summary: "",
      }
      groups.set(name, group)
    }
    return group
  }

  for (const entry of projection?.objects ?? []) {
    if (entry.object_type === "dataset" || entry.object_type === "custom_dataset") {
      const group = groupFor(entry.name)
      group.dataset = entry
      group.label = entry.label || entry.name
      group.isCte =
        entry.object_type === "custom_dataset" || entry.base.source_kind === "custom"
    } else if (entry.object_type === "field") {
      groupFor(entry.dataset || entry.name).fields.push(entry)
    } else {
      groupFor(relationshipDatasetName(entry)).relationships.push(entry)
    }
  }

  for (const group of groups.values()) {
    const children = [...group.fields, ...group.relationships]
    const pendingChildren = children.filter((entry) => entry.state !== "unchanged")
    const ownState = group.dataset?.state ?? "unchanged"
    group.pendingCount = pendingChildren.length + (ownState !== "unchanged" ? 1 : 0)
    if (children.some((entry) => entry.state === "conflict") || ownState === "conflict") {
      group.state = "conflict"
    } else if (ownState !== "unchanged") {
      group.state = ownState
    } else if (pendingChildren.length > 0) {
      group.state = "edited"
    } else {
      group.state = "unchanged"
    }
    group.summary = summarizeGroup(group, pendingChildren)
  }

  return [...groups.values()].sort(
    (a, b) => GROUP_STATE_ORDER[a.state] - GROUP_STATE_ORDER[b.state] || a.name.localeCompare(b.name),
  )
}

function summarizeGroup(group: CanvasDatasetGroup, pendingChildren: CanvasObjectEntry[]): string {
  const parts: string[] = []
  const dataset = group.dataset
  if (dataset?.state === "new") {
    parts.push(group.isCte ? "new SQL dataset" : "new dataset")
  } else if (dataset?.state === "deleted") {
    parts.push("will be removed")
  } else if (dataset && dataset.state !== "unchanged" && Object.keys(dataset.diff).length > 0) {
    parts.push(`${Object.keys(dataset.diff).sort().join(", ")} edited`)
  }
  const fieldChangeSummary = summarizeFieldChanges(
    pendingChildren.filter((entry) => entry.object_type === "field"),
  )
  const links = pendingChildren.filter((entry) => entry.object_type === "relationship").length
  parts.push(...fieldChangeSummary)
  if (links) parts.push(`${links} link${links === 1 ? "" : "s"}`)
  return parts.length > 0 ? parts.join(" · ") : "No pending changes"
}

function summarizeFieldChanges(fields: CanvasObjectEntry[]): string[] {
  const parts: string[] = []
  const states: Array<["new" | "edited" | "deleted", string]> = [
    ["new", "added"],
    ["edited", "edited"],
    ["deleted", "removed"],
  ]
  const kindOrder = ["measure", "dimension", "time dimension", "field"]
  for (const [state, verb] of states) {
    const byKind = new Map<string, number>()
    for (const entry of fields) {
      if (entry.state !== state) continue
      const noun = fieldKindLabel(entry)
      byKind.set(noun, (byKind.get(noun) ?? 0) + 1)
    }
    for (const noun of kindOrder) {
      const count = byKind.get(noun) ?? 0
      if (!count) continue
      parts.push(`${count} ${noun}${count === 1 ? "" : "s"} ${verb}`)
    }
  }
  return parts
}
