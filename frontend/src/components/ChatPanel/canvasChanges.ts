import type {
  DatasetCatalog,
  SemanticDataset,
  SemanticField,
} from "@/store/datasetSlice"

export type DatasetEditableKey = "name" | "label" | "description"
export type FieldEditableKey = "name" | "label" | "description"
export type RelationshipEditableKey = "name" | "relationship_type" | "join_expression"

export interface FieldPatch {
  name?: string
  label?: string
  description?: string
}

export interface RelationshipPatch {
  name?: string
  relationship_type?: string
  join_expression?: string
}

export interface DatasetPatch {
  dataset?: Partial<Record<DatasetEditableKey, string>>
  fields?: Record<string, FieldPatch>
  relationships?: Record<string, RelationshipPatch>
}

export interface SemanticCanvasChanges {
  datasets: Record<string, DatasetPatch>
}

export interface SemanticCanvasDiagnostic {
  severity: "error" | "warning" | string
  code: string
  message: string
}

export interface SemanticCanvasResponse {
  id: string
  status: string
  changes: unknown
  diagnostics: SemanticCanvasDiagnostic[]
  catalog: DatasetCatalog
  updated_at: string
}

export interface CanvasDiff {
  id: string
  datasetId: string
  datasetName: string
  targetType: "dataset" | "field" | "relationship"
  targetId: string
  targetLabel: string
  property: string
  from: string
  to: string
}

export const emptyCanvasChanges = (): SemanticCanvasChanges => ({ datasets: {} })

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

const cleanObject = <T extends object>(value: T): T | undefined => {
  const entries = Object.entries(value as Record<string, unknown>).filter(
    ([, item]) => item !== undefined
  )
  return entries.length > 0 ? (Object.fromEntries(entries) as T) : undefined
}

const normalizePatch = <T extends string>(
  value: unknown,
  allowedKeys: readonly T[],
): Partial<Record<T, string>> | undefined => {
  if (!isRecord(value)) return undefined

  const patch: Partial<Record<T, string>> = {}
  allowedKeys.forEach((key) => {
    const item = value[key]
    if (typeof item === "string") {
      patch[key] = item
    }
  })
  return cleanObject(patch)
}

const DATASET_KEYS = ["name", "label", "description"] as const
const FIELD_KEYS = ["name", "label", "description"] as const
const RELATIONSHIP_KEYS = ["name", "relationship_type", "join_expression"] as const

export function normalizeCanvasChanges(value: unknown): SemanticCanvasChanges {
  if (!isRecord(value) || !isRecord(value.datasets)) return emptyCanvasChanges()

  const datasets: Record<string, DatasetPatch> = {}

  Object.entries(value.datasets).forEach(([datasetId, rawPatch]) => {
    if (!isRecord(rawPatch)) return

    const dataset = normalizePatch(rawPatch.dataset, DATASET_KEYS)
    const fields: Record<string, FieldPatch> = {}
    const relationships: Record<string, RelationshipPatch> = {}

    if (isRecord(rawPatch.fields)) {
      Object.entries(rawPatch.fields).forEach(([fieldId, rawFieldPatch]) => {
        const patch = normalizePatch(rawFieldPatch, FIELD_KEYS)
        if (patch) fields[fieldId] = patch
      })
    }

    if (isRecord(rawPatch.relationships)) {
      Object.entries(rawPatch.relationships).forEach(([relationshipId, rawRelationshipPatch]) => {
        const patch = normalizePatch(rawRelationshipPatch, RELATIONSHIP_KEYS)
        if (patch) relationships[relationshipId] = patch
      })
    }

    const nextPatch: DatasetPatch = {}
    if (dataset) nextPatch.dataset = dataset
    if (Object.keys(fields).length > 0) nextPatch.fields = fields
    if (Object.keys(relationships).length > 0) nextPatch.relationships = relationships

    if (Object.keys(nextPatch).length > 0) {
      datasets[datasetId] = nextPatch
    }
  })

  return { datasets }
}

const patchedField = (
  field: SemanticField,
  datasetName: string,
  patch: FieldPatch | undefined,
): SemanticField => {
  const name = patch?.name ?? field.name
  return {
    ...field,
    ...patch,
    name,
    label: patch?.label ?? field.label,
    description: patch?.description ?? field.description,
    member: `${datasetName}.${name}`,
  }
}

export function applyCanvasChanges(
  dataset: SemanticDataset,
  changes: SemanticCanvasChanges,
): SemanticDataset {
  const patch = changes.datasets[dataset.id]
  if (!patch) return dataset

  const datasetName = patch.dataset?.name ?? dataset.name

  return {
    ...dataset,
    ...patch.dataset,
    name: datasetName,
    label: patch.dataset?.label ?? dataset.label,
    description: patch.dataset?.description ?? dataset.description,
    dimensions: dataset.dimensions.map((field) =>
      patchedField(field, datasetName, patch.fields?.[field.id])
    ),
    time_dimensions: dataset.time_dimensions.map((field) =>
      patchedField(field, datasetName, patch.fields?.[field.id])
    ),
    measures: dataset.measures.map((field) =>
      patchedField(field, datasetName, patch.fields?.[field.id])
    ),
    relationships: dataset.relationships.map((relationship) => ({
      ...relationship,
      ...patch.relationships?.[relationship.id],
      from_dataset: datasetName,
    })),
  }
}

function cloneChanges(changes: SemanticCanvasChanges): SemanticCanvasChanges {
  return {
    datasets: Object.fromEntries(
      Object.entries(changes.datasets).map(([datasetId, patch]) => [
        datasetId,
        {
          dataset: patch.dataset ? { ...patch.dataset } : undefined,
          fields: patch.fields
            ? Object.fromEntries(
                Object.entries(patch.fields).map(([fieldId, fieldPatch]) => [
                  fieldId,
                  { ...fieldPatch },
                ])
              )
            : undefined,
          relationships: patch.relationships
            ? Object.fromEntries(
                Object.entries(patch.relationships).map(([relationshipId, relationshipPatch]) => [
                  relationshipId,
                  { ...relationshipPatch },
                ])
              )
            : undefined,
        },
      ])
    ),
  }
}

function cleanupChanges(changes: SemanticCanvasChanges): SemanticCanvasChanges {
  const datasets: Record<string, DatasetPatch> = {}

  Object.entries(changes.datasets).forEach(([datasetId, patch]) => {
    const dataset = patch.dataset ? cleanObject(patch.dataset) : undefined
    const fieldEntries: [string, FieldPatch][] = []
    Object.entries(patch.fields ?? {}).forEach(([fieldId, fieldPatch]) => {
      const cleaned = cleanObject(fieldPatch)
      if (cleaned) fieldEntries.push([fieldId, cleaned])
    })
    const relationshipEntries: [string, RelationshipPatch][] = []
    Object.entries(patch.relationships ?? {}).forEach(([relationshipId, relationshipPatch]) => {
      const cleaned = cleanObject(relationshipPatch)
      if (cleaned) relationshipEntries.push([relationshipId, cleaned])
    })
    const fields = fieldEntries.length > 0 ? Object.fromEntries(fieldEntries) : undefined
    const relationships =
      relationshipEntries.length > 0 ? Object.fromEntries(relationshipEntries) : undefined

    const nextPatch: DatasetPatch = {}
    if (dataset) nextPatch.dataset = dataset
    if (fields && Object.keys(fields).length > 0) nextPatch.fields = fields
    if (relationships && Object.keys(relationships).length > 0) {
      nextPatch.relationships = relationships
    }

    if (Object.keys(nextPatch).length > 0) {
      datasets[datasetId] = nextPatch
    }
  })

  return { datasets }
}

export function updateDatasetPatch(
  changes: SemanticCanvasChanges,
  datasetId: string,
  key: DatasetEditableKey,
  value: string,
  originalValue: string,
): SemanticCanvasChanges {
  const next = cloneChanges(changes)
  next.datasets[datasetId] = next.datasets[datasetId] ?? {}
  next.datasets[datasetId].dataset = next.datasets[datasetId].dataset ?? {}

  if (value === originalValue) {
    delete next.datasets[datasetId].dataset?.[key]
  } else {
    next.datasets[datasetId].dataset[key] = value
  }

  return cleanupChanges(next)
}

export function updateFieldPatch(
  changes: SemanticCanvasChanges,
  datasetId: string,
  fieldId: string,
  key: FieldEditableKey,
  value: string,
  originalValue: string,
): SemanticCanvasChanges {
  const next = cloneChanges(changes)
  next.datasets[datasetId] = next.datasets[datasetId] ?? {}
  next.datasets[datasetId].fields = next.datasets[datasetId].fields ?? {}
  next.datasets[datasetId].fields[fieldId] = next.datasets[datasetId].fields[fieldId] ?? {}

  if (value === originalValue) {
    delete next.datasets[datasetId].fields?.[fieldId]?.[key]
  } else {
    next.datasets[datasetId].fields[fieldId][key] = value
  }

  return cleanupChanges(next)
}

export function updateRelationshipPatch(
  changes: SemanticCanvasChanges,
  datasetId: string,
  relationshipId: string,
  key: RelationshipEditableKey,
  value: string,
  originalValue: string,
): SemanticCanvasChanges {
  const next = cloneChanges(changes)
  next.datasets[datasetId] = next.datasets[datasetId] ?? {}
  next.datasets[datasetId].relationships = next.datasets[datasetId].relationships ?? {}
  next.datasets[datasetId].relationships[relationshipId] =
    next.datasets[datasetId].relationships[relationshipId] ?? {}

  if (value === originalValue) {
    delete next.datasets[datasetId].relationships?.[relationshipId]?.[key]
  } else {
    next.datasets[datasetId].relationships[relationshipId][key] = value
  }

  return cleanupChanges(next)
}

export function revertDatasetPatch(
  changes: SemanticCanvasChanges,
  datasetId: string,
): SemanticCanvasChanges {
  const next = cloneChanges(changes)
  delete next.datasets[datasetId]
  return cleanupChanges(next)
}

export function revertFieldPatch(
  changes: SemanticCanvasChanges,
  datasetId: string,
  fieldId: string,
): SemanticCanvasChanges {
  const next = cloneChanges(changes)
  delete next.datasets[datasetId]?.fields?.[fieldId]
  return cleanupChanges(next)
}

export function revertRelationshipPatch(
  changes: SemanticCanvasChanges,
  datasetId: string,
  relationshipId: string,
): SemanticCanvasChanges {
  const next = cloneChanges(changes)
  delete next.datasets[datasetId]?.relationships?.[relationshipId]
  return cleanupChanges(next)
}

const displayValue = (value: unknown): string => {
  if (value === null || value === undefined || value === "") return "(blank)"
  return String(value)
}

const fieldLookup = (dataset: SemanticDataset): Record<string, SemanticField> =>
  Object.fromEntries(
    [...dataset.measures, ...dataset.time_dimensions, ...dataset.dimensions].map((field) => [
      field.id,
      field,
    ])
  )

const diffFromPatch = (
  args: Omit<CanvasDiff, "id" | "from" | "to"> & {
    property: string
    fromValue: unknown
    toValue: unknown
  },
): CanvasDiff | null => {
  const from = displayValue(args.fromValue)
  const to = displayValue(args.toValue)
  if (from === to) return null

  return {
    id: `${args.targetType}:${args.targetId}:${args.property}`,
    datasetId: args.datasetId,
    datasetName: args.datasetName,
    targetType: args.targetType,
    targetId: args.targetId,
    targetLabel: args.targetLabel,
    property: args.property,
    from,
    to,
  }
}

export function collectCanvasDiffs(
  catalog: DatasetCatalog | null,
  changes: SemanticCanvasChanges,
): CanvasDiff[] {
  if (!catalog) return []

  const diffs: CanvasDiff[] = []

  Object.entries(changes.datasets).forEach(([datasetId, patch]) => {
    const dataset = catalog.datasets.find((item) => item.id === datasetId)
    if (!dataset) return
    const patchedDatasetName = patch.dataset?.name ?? dataset.name
    const datasetLabel = patch.dataset?.label ?? dataset.label ?? patchedDatasetName

    Object.entries(patch.dataset ?? {}).forEach(([property, toValue]) => {
      const diff = diffFromPatch({
        datasetId,
        datasetName: patchedDatasetName,
        targetType: "dataset",
        targetId: datasetId,
        targetLabel: dataset.label || dataset.name,
        property,
        fromValue: dataset[property as DatasetEditableKey],
        toValue,
      })
      if (diff) diffs.push(diff)
    })

    const fields = fieldLookup(dataset)
    Object.entries(patch.fields ?? {}).forEach(([fieldId, fieldPatch]) => {
      const field = fields[fieldId]
      if (!field) return
      Object.entries(fieldPatch).forEach(([property, toValue]) => {
        const diff = diffFromPatch({
          datasetId,
          datasetName: patchedDatasetName,
          targetType: "field",
          targetId: fieldId,
          targetLabel: field.label || field.name,
          property,
          fromValue: field[property as FieldEditableKey],
          toValue,
        })
        if (diff) diffs.push(diff)
      })
    })

    Object.entries(patch.relationships ?? {}).forEach(([relationshipId, relationshipPatch]) => {
      const relationship = dataset.relationships.find((item) => item.id === relationshipId)
      if (!relationship) return
      Object.entries(relationshipPatch).forEach(([property, toValue]) => {
        const diff = diffFromPatch({
          datasetId,
          datasetName: datasetLabel,
          targetType: "relationship",
          targetId: relationshipId,
          targetLabel: relationship.name,
          property,
          fromValue: relationship[property as RelationshipEditableKey],
          toValue,
        })
        if (diff) diffs.push(diff)
      })
    })
  })

  return diffs
}

export function countDiffsByDataset(diffs: CanvasDiff[]): Record<string, number> {
  return diffs.reduce<Record<string, number>>((counts, diff) => {
    counts[diff.datasetId] = (counts[diff.datasetId] ?? 0) + 1
    return counts
  }, {})
}

export function hasCanvasChanges(changes: SemanticCanvasChanges): boolean {
  return Object.keys(changes.datasets).length > 0
}
