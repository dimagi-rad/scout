import { useCallback, useEffect, useMemo, useState } from "react"
import type { ReactNode } from "react"
import {
  Check,
  CircleAlert,
  Database,
  Loader2,
  RefreshCw,
  RotateCcw,
  Save,
  Sigma,
} from "lucide-react"
import { api } from "@/api/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"
import type { SemanticDataset, SemanticField } from "@/store/datasetSlice"
import {
  applyCanvasChanges,
  collectCanvasDiffs,
  emptyCanvasChanges,
  hasCanvasChanges,
  normalizeCanvasChanges,
  revertDatasetPatch,
  revertFieldPatch,
  updateDatasetPatch,
  updateFieldPatch,
  type CanvasDiff,
  type DatasetEditableKey,
  type FieldEditableKey,
  type SemanticCanvasChanges,
  type SemanticCanvasResponse,
} from "./canvasChanges"

interface ChatCanvasPanelProps {
  workspaceId: string
}

type LoadStatus = "idle" | "loading" | "loaded" | "error"
type SaveStatus = "idle" | "saving" | "saved" | "error"

const EMPTY_DATASETS: SemanticDataset[] = []

export function ChatCanvasPanel({ workspaceId }: ChatCanvasPanelProps) {
  const [status, setStatus] = useState<LoadStatus>("loading")
  const [error, setError] = useState<string | null>(null)
  const [canvas, setCanvas] = useState<SemanticCanvasResponse | null>(null)
  const [changes, setChanges] = useState<SemanticCanvasChanges>(emptyCanvasChanges)
  const [requestedDatasetId, setRequestedDatasetId] = useState<string | null>(null)
  const [saveStatus, setSaveStatus] = useState<SaveStatus>("idle")

  const loadCanvas = useCallback(async () => {
    setStatus("loading")
    setError(null)
    try {
      const response = await api.get<SemanticCanvasResponse>(
        `/api/workspaces/${workspaceId}/semantic-canvas/`
      )
      setCanvas(response)
      setChanges(normalizeCanvasChanges(response.changes))
      setStatus("loaded")
    } catch (loadError) {
      setStatus("error")
      setError(loadError instanceof Error ? loadError.message : "Failed to load canvas")
    }
  }, [workspaceId])

  useEffect(() => {
    let cancelled = false

    api.get<SemanticCanvasResponse>(`/api/workspaces/${workspaceId}/semantic-canvas/`)
      .then((response) => {
        if (cancelled) return
        setCanvas(response)
        setChanges(normalizeCanvasChanges(response.changes))
        setStatus("loaded")
      })
      .catch((loadError: unknown) => {
        if (cancelled) return
        setStatus("error")
        setError(loadError instanceof Error ? loadError.message : "Failed to load canvas")
      })

    return () => {
      cancelled = true
    }
  }, [workspaceId])

  const catalog = canvas?.catalog ?? null
  const datasets = catalog?.datasets ?? EMPTY_DATASETS
  const selectedDatasetId =
    requestedDatasetId && datasets.some((dataset) => dataset.id === requestedDatasetId)
      ? requestedDatasetId
      : datasets[0]?.id ?? null

  const remoteChanges = useMemo(() => normalizeCanvasChanges(canvas?.changes), [canvas?.changes])
  const dirty = JSON.stringify(remoteChanges) !== JSON.stringify(changes)
  const diffs = useMemo(() => collectCanvasDiffs(catalog, changes), [catalog, changes])

  const selectedBaseDataset = useMemo(
    () => datasets.find((dataset) => dataset.id === selectedDatasetId) ?? null,
    [datasets, selectedDatasetId]
  )
  const selectedDataset = useMemo(
    () => (selectedBaseDataset ? applyCanvasChanges(selectedBaseDataset, changes) : null),
    [changes, selectedBaseDataset]
  )

  const saveChanges = useCallback(
    async (nextChanges: SemanticCanvasChanges) => {
      setSaveStatus("saving")
      setError(null)
      try {
        const response = await api.post<SemanticCanvasResponse>(
          `/api/workspaces/${workspaceId}/semantic-canvas/`,
          { changes: nextChanges }
        )
        setCanvas(response)
        setChanges(normalizeCanvasChanges(response.changes))
        setSaveStatus("saved")
      } catch (saveError) {
        setSaveStatus("error")
        setError(saveError instanceof Error ? saveError.message : "Failed to save canvas")
      }
    },
    [workspaceId]
  )

  const stageDatasetValue = (key: DatasetEditableKey, value: string) => {
    if (!selectedBaseDataset) return
    setChanges((current) =>
      updateDatasetPatch(
        current,
        selectedBaseDataset.id,
        key,
        value,
        selectedBaseDataset[key] ?? ""
      )
    )
    setSaveStatus("idle")
  }

  const stageFieldValue = (field: SemanticField, key: FieldEditableKey, value: string) => {
    if (!selectedBaseDataset) return
    setChanges((current) =>
      updateFieldPatch(current, selectedBaseDataset.id, field.id, key, value, field[key] ?? "")
    )
    setSaveStatus("idle")
  }

  return (
    <aside className="hidden h-full w-[25rem] shrink-0 flex-col border-l bg-background lg:flex">
      <div className="flex h-12 items-center justify-between gap-3 border-b px-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Database className="h-4 w-4 text-muted-foreground" />
            <h2 className="truncate text-sm font-semibold">Canvas</h2>
            {diffs.length > 0 && <Badge variant="secondary">{diffs.length}</Badge>}
          </div>
          <p className="truncate text-xs text-muted-foreground">
            {catalog ? `${datasets.length} datasets` : "Semantic model"}
          </p>
        </div>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon-xs"
            onClick={() => void loadCanvas()}
            disabled={status === "loading"}
            aria-label="Refresh canvas"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", status === "loading" && "animate-spin")} />
          </Button>
          <Button
            variant="outline"
            size="xs"
            onClick={() => void saveChanges(emptyCanvasChanges())}
            disabled={!hasCanvasChanges(changes) || saveStatus === "saving"}
          >
            <RotateCcw className="h-3.5 w-3.5" />
            Clear
          </Button>
          <Button
            size="xs"
            onClick={() => void saveChanges(changes)}
            disabled={!dirty || saveStatus === "saving"}
          >
            {saveStatus === "saving" ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Save className="h-3.5 w-3.5" />
            )}
            Save
          </Button>
        </div>
      </div>

      {status === "loading" && !canvas ? (
        <PanelState icon={<Loader2 className="h-5 w-5 animate-spin" />} title="Loading canvas" />
      ) : status === "error" ? (
        <PanelState
          icon={<CircleAlert className="h-5 w-5" />}
          title="Canvas unavailable"
          body={error ?? "Could not load the semantic canvas."}
          action={<Button size="sm" onClick={() => void loadCanvas()}>Try Again</Button>}
        />
      ) : datasets.length === 0 ? (
        <PanelState
          icon={<Database className="h-5 w-5" />}
          title="No datasets"
          body="Load workspace data from chat before editing the semantic model."
        />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="space-y-4 p-3">
            <StatusRow saveStatus={saveStatus} dirty={dirty} error={error} />

            <div className="space-y-1.5">
              <Label className="text-xs text-muted-foreground">Dataset</Label>
              <Select value={selectedDatasetId ?? undefined} onValueChange={setRequestedDatasetId}>
                <SelectTrigger className="h-8 w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {datasets.map((dataset) => (
                    <SelectItem key={dataset.id} value={dataset.id}>
                      {applyCanvasChanges(dataset, changes).label || dataset.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

                <PendingChanges diffs={diffs} onSelectDataset={setRequestedDatasetId} />

            {selectedBaseDataset && selectedDataset && (
              <>
                <DatasetEditor
                  baseDataset={selectedBaseDataset}
                  dataset={selectedDataset}
                  onChange={stageDatasetValue}
                  onRevert={() => {
                    setChanges((current) => revertDatasetPatch(current, selectedBaseDataset.id))
                    setSaveStatus("idle")
                  }}
                />
                <FieldsEditor
                  title="Measures"
                  icon={<Sigma className="h-4 w-4" />}
                  baseDataset={selectedBaseDataset}
                  baseFields={selectedBaseDataset.measures}
                  fields={selectedDataset.measures}
                  onChange={stageFieldValue}
                  onRevert={(fieldId) => {
                    setChanges((current) =>
                      revertFieldPatch(current, selectedBaseDataset.id, fieldId)
                    )
                    setSaveStatus("idle")
                  }}
                />
                <FieldsEditor
                  title="Dimensions"
                  icon={<Database className="h-4 w-4" />}
                  baseDataset={selectedBaseDataset}
                  baseFields={[
                    ...selectedBaseDataset.time_dimensions,
                    ...selectedBaseDataset.dimensions,
                  ]}
                  fields={[...selectedDataset.time_dimensions, ...selectedDataset.dimensions]}
                  onChange={stageFieldValue}
                  onRevert={(fieldId) => {
                    setChanges((current) =>
                      revertFieldPatch(current, selectedBaseDataset.id, fieldId)
                    )
                    setSaveStatus("idle")
                  }}
                />
              </>
            )}
          </div>
        </div>
      )}
    </aside>
  )
}

function StatusRow({
  saveStatus,
  dirty,
  error,
}: {
  saveStatus: SaveStatus
  dirty: boolean
  error: string | null
}) {
  if (saveStatus === "error") {
    return (
      <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">
        {error ?? "Canvas save failed."}
      </div>
    )
  }
  if (saveStatus === "saved" && !dirty) {
    return (
      <div className="flex items-center gap-2 rounded-md border px-3 py-2 text-xs text-muted-foreground">
        <Check className="h-3.5 w-3.5" />
        Saved
      </div>
    )
  }
  if (dirty) {
    return (
      <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
        Unsaved canvas changes
      </div>
    )
  }
  return null
}

function PendingChanges({
  diffs,
  onSelectDataset,
}: {
  diffs: CanvasDiff[]
  onSelectDataset: (datasetId: string) => void
}) {
  return (
    <section className="rounded-md border">
      <div className="flex items-center justify-between border-b px-3 py-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Pending
        </h3>
        <Badge variant="secondary">{diffs.length}</Badge>
      </div>
      {diffs.length === 0 ? (
        <div className="px-3 py-4 text-sm text-muted-foreground">
          No pending edits.
        </div>
      ) : (
        <div className="max-h-52 overflow-y-auto divide-y">
          {diffs.map((diff) => (
            <button
              key={diff.id}
              type="button"
              onClick={() => onSelectDataset(diff.datasetId)}
              className="block w-full px-3 py-2 text-left hover:bg-accent"
            >
              <div className="truncate text-sm font-medium">{diff.targetLabel}</div>
              <div className="text-xs text-muted-foreground">
                {diff.targetType}.{diff.property}
              </div>
              <div className="mt-1 grid grid-cols-[2rem_minmax(0,1fr)] gap-2 text-xs">
                <span className="text-muted-foreground">To</span>
                <span className="truncate font-medium">{diff.to}</span>
              </div>
            </button>
          ))}
        </div>
      )}
    </section>
  )
}

function DatasetEditor({
  baseDataset,
  dataset,
  onChange,
  onRevert,
}: {
  baseDataset: SemanticDataset
  dataset: SemanticDataset
  onChange: (key: DatasetEditableKey, value: string) => void
  onRevert: () => void
}) {
  const hasChanges =
    baseDataset.name !== dataset.name ||
    baseDataset.label !== dataset.label ||
    baseDataset.description !== dataset.description

  return (
    <section className="rounded-md border">
      <SectionHeader title="Dataset" count={hasChanges ? 1 : 0} onRevert={onRevert} />
      <div className="space-y-3 p-3">
        <TextControl
          label="Name"
          value={dataset.name}
          originalValue={baseDataset.name}
          onChange={(value) => onChange("name", slugValue(value))}
        />
        <TextControl
          label="Label"
          value={dataset.label}
          originalValue={baseDataset.label}
          onChange={(value) => onChange("label", value)}
        />
        <TextAreaControl
          label="Description"
          value={dataset.description}
          originalValue={baseDataset.description}
          onChange={(value) => onChange("description", value)}
        />
      </div>
    </section>
  )
}

function FieldsEditor({
  title,
  icon,
  baseDataset,
  baseFields,
  fields,
  onChange,
  onRevert,
}: {
  title: string
  icon: ReactNode
  baseDataset: SemanticDataset
  baseFields: SemanticField[]
  fields: SemanticField[]
  onChange: (field: SemanticField, key: FieldEditableKey, value: string) => void
  onRevert: (fieldId: string) => void
}) {
  const fieldsById = useMemo(
    () => Object.fromEntries(fields.map((field) => [field.id, field])),
    [fields]
  )

  return (
    <section className="rounded-md border">
      <div className="flex items-center gap-2 border-b px-3 py-2">
        {icon}
        <h3 className="text-sm font-semibold">{title}</h3>
        <Badge variant="secondary">{baseFields.length}</Badge>
      </div>
      {baseFields.length === 0 ? (
        <div className="px-3 py-4 text-sm text-muted-foreground">
          No {title.toLowerCase()} defined.
        </div>
      ) : (
        <div className="divide-y">
          {baseFields.map((baseField) => {
            const field = fieldsById[baseField.id] ?? baseField
            const hasChanges =
              baseField.name !== field.name ||
              baseField.label !== field.label ||
              baseField.description !== field.description

            return (
              <div key={baseField.id} className={cn("space-y-2 p-3", hasChanges && "bg-amber-50")}>
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex min-w-0 items-center gap-2">
                      <code className="truncate rounded bg-muted px-1.5 py-0.5 text-xs">
                        {baseDataset.name}.{baseField.name}
                      </code>
                      {hasChanges && <Badge variant="secondary">Edited</Badge>}
                    </div>
                    <p className="mt-1 truncate text-xs text-muted-foreground">
                      Preview: {field.member}
                    </p>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    onClick={() => onRevert(baseField.id)}
                    disabled={!hasChanges}
                    aria-label={`Revert ${baseField.name}`}
                  >
                    <RotateCcw className="h-3.5 w-3.5" />
                  </Button>
                </div>
                <div className="grid grid-cols-[8rem_minmax(0,1fr)] gap-2">
                  <TextControl
                    label="Name"
                    value={field.name}
                    originalValue={baseField.name}
                    onChange={(value) => onChange(baseField, "name", slugValue(value))}
                  />
                  <TextControl
                    label="Label"
                    value={field.label}
                    originalValue={baseField.label}
                    onChange={(value) => onChange(baseField, "label", value)}
                  />
                </div>
                <TextAreaControl
                  label="Description"
                  value={field.description}
                  originalValue={baseField.description}
                  onChange={(value) => onChange(baseField, "description", value)}
                />
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

function SectionHeader({
  title,
  count,
  onRevert,
}: {
  title: string
  count: number
  onRevert: () => void
}) {
  return (
    <div className="flex items-center justify-between gap-2 border-b px-3 py-2">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        {count > 0 && <Badge variant="secondary">{count}</Badge>}
      </div>
      <Button variant="ghost" size="icon-xs" onClick={onRevert} disabled={count === 0}>
        <RotateCcw className="h-3.5 w-3.5" />
      </Button>
    </div>
  )
}

function TextControl({
  label,
  value,
  originalValue,
  onChange,
}: {
  label: string
  value: string
  originalValue: string
  onChange: (value: string) => void
}) {
  const changed = value !== originalValue
  return (
    <div className="space-y-1.5">
      <Label className="text-xs text-muted-foreground">{label}</Label>
      <Input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className={cn("h-8", changed && "border-amber-400 bg-amber-50")}
      />
    </div>
  )
}

function TextAreaControl({
  label,
  value,
  originalValue,
  onChange,
}: {
  label: string
  value: string
  originalValue: string
  onChange: (value: string) => void
}) {
  const changed = value !== originalValue
  return (
    <div className="space-y-1.5">
      <Label className="text-xs text-muted-foreground">{label}</Label>
      <Textarea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className={cn("min-h-14 resize-y", changed && "border-amber-400 bg-amber-50")}
      />
    </div>
  )
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

function slugValue(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "")
}
