import { useState, useEffect, useCallback, useRef } from "react"
import { useAppStore } from "@/store/store"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Badge } from "@/components/ui/badge"
import type { TableDetail as TableDetailType, TableAnnotations } from "@/store/dictionarySlice"

interface TableDetailProps {
  projectId: string
  table: TableDetailType
}

// Debounce hook
function useDebounce<T>(value: T, delay: number): T {
  const [debouncedValue, setDebouncedValue] = useState<T>(value)

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedValue(value)
    }, delay)

    return () => {
      clearTimeout(timer)
    }
  }, [value, delay])

  return debouncedValue
}

export function TableDetail({ projectId, table }: TableDetailProps) {
  const updateAnnotations = useAppStore((s) => s.dictionaryActions.updateAnnotations)

  // Local state for form fields
  const [description, setDescription] = useState("")
  const [useCases, setUseCases] = useState("")
  const [dataQualityNotes, setDataQualityNotes] = useState("")
  const [refreshFrequency, setRefreshFrequency] = useState("")
  const [owner, setOwner] = useState("")
  const [columnNotes, setColumnNotes] = useState<Record<string, string>>({})

  // Track if we've initialized from props
  const initializedRef = useRef<string | null>(null)
  const tableKey = `${table.schema}.${table.table}`

  // Initialize form when table changes
  useEffect(() => {
    if (initializedRef.current !== tableKey) {
      const annotations = table.annotations
      setDescription(annotations?.description ?? "")
      setUseCases(annotations?.use_cases ?? "")
      setDataQualityNotes(annotations?.data_quality_notes ?? "")
      setRefreshFrequency(annotations?.refresh_frequency ?? "")
      setOwner(annotations?.owner ?? "")
      setColumnNotes(annotations?.column_notes ?? {})
      initializedRef.current = tableKey
    }
  }, [table, tableKey])

  // Debounced values for auto-save
  const debouncedDescription = useDebounce(description, 1000)
  const debouncedUseCases = useDebounce(useCases, 1000)
  const debouncedDataQualityNotes = useDebounce(dataQualityNotes, 1000)
  const debouncedRefreshFrequency = useDebounce(refreshFrequency, 1000)
  const debouncedOwner = useDebounce(owner, 1000)
  const debouncedColumnNotes = useDebounce(columnNotes, 1000)

  // Auto-save when debounced values change
  const saveAnnotations = useCallback(async () => {
    if (initializedRef.current !== tableKey) return

    const annotations: Partial<TableAnnotations> = {
      description: debouncedDescription,
      use_cases: debouncedUseCases,
      data_quality_notes: debouncedDataQualityNotes,
      refresh_frequency: debouncedRefreshFrequency,
      owner: debouncedOwner,
      column_notes: debouncedColumnNotes,
    }

    try {
      await updateAnnotations(projectId, table.schema, table.table, annotations)
    } catch (error) {
      console.error("Failed to save annotations:", error)
    }
  }, [
    projectId,
    table.schema,
    table.table,
    tableKey,
    debouncedDescription,
    debouncedUseCases,
    debouncedDataQualityNotes,
    debouncedRefreshFrequency,
    debouncedOwner,
    debouncedColumnNotes,
    updateAnnotations,
  ])

  // Track if any debounced value has changed from the initial value
  const hasChangedRef = useRef(false)

  useEffect(() => {
    // Skip first render after initialization
    if (!hasChangedRef.current) {
      hasChangedRef.current = true
      return
    }
    saveAnnotations()
  }, [saveAnnotations])

  const updateColumnNote = (columnName: string, note: string) => {
    setColumnNotes((prev) => ({
      ...prev,
      [columnName]: note,
    }))
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Header */}
      <div className="border-b p-4">
        <div className="flex items-center gap-2">
          <Badge variant="outline">{table.schema}</Badge>
          <h2 className="text-xl font-semibold">{table.table}</h2>
        </div>
        <p className="mt-1 text-sm text-muted-foreground">
          {table.columns.length} columns
        </p>
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {/* Columns Table */}
        <div className="mb-6">
          <h3 className="mb-3 text-sm font-medium">Columns</h3>
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[200px]">Name</TableHead>
                  <TableHead className="w-[120px]">Type</TableHead>
                  <TableHead className="w-[80px]">Nullable</TableHead>
                  <TableHead>Description</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {table.columns.map((column) => (
                  <TableRow key={column.name}>
                    <TableCell className="font-mono text-sm">
                      {column.name}
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary" className="font-mono text-xs">
                        {column.type}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      {column.nullable ? (
                        <span className="text-muted-foreground">Yes</span>
                      ) : (
                        <span className="font-medium">No</span>
                      )}
                    </TableCell>
                    <TableCell>
                      <Input
                        placeholder="Add description..."
                        value={columnNotes[column.name] ?? column.description ?? ""}
                        onChange={(e) => updateColumnNote(column.name, e.target.value)}
                        className="h-8 border-0 bg-transparent px-0 focus-visible:ring-0"
                      />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>

        {/* Annotation Fields */}
        <div className="space-y-4">
          <h3 className="text-sm font-medium">Table Annotations</h3>

          <div className="grid gap-4">
            <div className="space-y-2">
              <Label htmlFor="description">Description</Label>
              <Textarea
                id="description"
                placeholder="Describe what this table contains..."
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={3}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="use_cases">Use Cases</Label>
              <Textarea
                id="use_cases"
                placeholder="Common use cases and queries..."
                value={useCases}
                onChange={(e) => setUseCases(e.target.value)}
                rows={2}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="data_quality_notes">Data Quality Notes</Label>
              <Textarea
                id="data_quality_notes"
                placeholder="Known data quality issues or considerations..."
                value={dataQualityNotes}
                onChange={(e) => setDataQualityNotes(e.target.value)}
                rows={2}
              />
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="refresh_frequency">Refresh Frequency</Label>
                <Input
                  id="refresh_frequency"
                  placeholder="e.g., Daily, Hourly, Real-time"
                  value={refreshFrequency}
                  onChange={(e) => setRefreshFrequency(e.target.value)}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="owner">Owner</Label>
                <Input
                  id="owner"
                  placeholder="Team or person responsible"
                  value={owner}
                  onChange={(e) => setOwner(e.target.value)}
                />
              </div>
            </div>
          </div>

          <p className="text-xs text-muted-foreground">
            Changes are saved automatically
          </p>
        </div>
      </div>
    </div>
  )
}
