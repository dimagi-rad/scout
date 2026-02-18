import { useState, useMemo } from "react"
import { ChevronDown, ChevronRight, Table2, Database } from "lucide-react"
import { cn } from "@/lib/utils"
import { Input } from "@/components/ui/input"
import type { DataDictionary } from "@/store/dictionarySlice"

interface SchemaTreeProps {
  dictionary: DataDictionary
  selectedTable: { schema: string; table: string } | null
  onSelectTable: (schema: string, table: string) => void
}

export function SchemaTree({ dictionary, selectedTable, onSelectTable }: SchemaTreeProps) {
  const [searchQuery, setSearchQuery] = useState("")
  const [expandedSchemas, setExpandedSchemas] = useState<Set<string>>(new Set())

  const schemas = useMemo(() => dictionary?.schemas ?? {}, [dictionary?.schemas])

  // Filter tables based on search query
  const filteredSchemas = useMemo(() => {
    if (!searchQuery.trim()) {
      return schemas
    }

    const query = searchQuery.toLowerCase()
    const result: Record<string, Record<string, typeof schemas[string][string]>> = {}

    for (const [schemaName, tables] of Object.entries(schemas)) {
      const matchingTables: Record<string, typeof tables[string]> = {}

      for (const [tableName, tableInfo] of Object.entries(tables)) {
        if (
          tableName.toLowerCase().includes(query) ||
          schemaName.toLowerCase().includes(query)
        ) {
          matchingTables[tableName] = tableInfo
        }
      }

      if (Object.keys(matchingTables).length > 0) {
        result[schemaName] = matchingTables
      }
    }

    return result
  }, [schemas, searchQuery])

  // When searching, auto-expand all matching schemas; otherwise use manual toggle state
  const effectiveExpandedSchemas = searchQuery.trim()
    ? new Set(Object.keys(filteredSchemas))
    : expandedSchemas

  const toggleSchema = (schemaName: string) => {
    setExpandedSchemas((prev) => {
      const next = new Set(prev)
      if (next.has(schemaName)) {
        next.delete(schemaName)
      } else {
        next.add(schemaName)
      }
      return next
    })
  }

  const hasAnnotations = (tableInfo: typeof schemas[string][string]) => {
    return tableInfo.annotations && (
      tableInfo.annotations.description ||
      tableInfo.annotations.use_cases ||
      tableInfo.annotations.data_quality_notes ||
      tableInfo.annotations.owner
    )
  }

  return (
    <div className="flex h-full flex-col">
      <div className="p-3 border-b">
        <Input
          placeholder="Search tables..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="h-8"
          data-testid="table-search"
        />
      </div>

      <div className="flex-1 overflow-y-auto p-2">
        {Object.keys(filteredSchemas).length === 0 ? (
          <div className="px-2 py-4 text-sm text-muted-foreground text-center">
            {searchQuery ? "No tables found" : "No schemas available"}
          </div>
        ) : (
          Object.entries(filteredSchemas).map(([schemaName, tables]) => (
            <div key={schemaName} className="mb-1">
              <button
                onClick={() => toggleSchema(schemaName)}
                className="flex w-full items-center gap-1 rounded px-2 py-1.5 text-sm font-medium hover:bg-accent"
                data-testid={`schema-group-${schemaName}`}
              >
                {effectiveExpandedSchemas.has(schemaName) ? (
                  <ChevronDown className="h-4 w-4 text-muted-foreground" />
                ) : (
                  <ChevronRight className="h-4 w-4 text-muted-foreground" />
                )}
                <Database className="h-4 w-4 text-muted-foreground" />
                <span className="truncate">{schemaName}</span>
                <span className="ml-auto text-xs text-muted-foreground">
                  {Object.keys(tables).length}
                </span>
              </button>

              {effectiveExpandedSchemas.has(schemaName) && (
                <div className="ml-4 mt-1 space-y-0.5">
                  {Object.entries(tables).map(([tableName, tableInfo]) => {
                    const isSelected =
                      selectedTable?.schema === schemaName &&
                      selectedTable?.table === tableName
                    const annotated = hasAnnotations(tableInfo)

                    return (
                      <button
                        key={tableName}
                        onClick={() => onSelectTable(schemaName, tableName)}
                        className={cn(
                          "flex w-full items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-accent",
                          isSelected && "bg-accent"
                        )}
                        data-testid={`table-item-${tableName}`}
                      >
                        <Table2 className="h-4 w-4 text-muted-foreground" />
                        <span className="truncate">{tableName}</span>
                        {annotated && (
                          <span className="ml-auto h-2 w-2 rounded-full bg-primary" data-testid={`annotation-indicator-${tableName}`} />
                        )}
                      </button>
                    )
                  })}
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  )
}
