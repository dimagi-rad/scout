import { useEffect, useState } from "react"
import { RefreshCw, Database } from "lucide-react"
import { useAppStore } from "@/store/store"
import { Button } from "@/components/ui/button"
import { SchemaTree } from "./SchemaTree"
import { TableDetail } from "./TableDetail"

export function DataDictionaryPage() {
  const dataDictionary = useAppStore((s) => s.dataDictionary)
  const dictionaryStatus = useAppStore((s) => s.dictionaryStatus)
  const selectedTable = useAppStore((s) => s.selectedTable)
  const { fetchDictionary, refreshSchema, fetchTable, clearDictionary } =
    useAppStore((s) => s.dictionaryActions)

  const [isRefreshing, setIsRefreshing] = useState(false)

  // Fetch dictionary on mount
  useEffect(() => {
    fetchDictionary()
    return () => {
      clearDictionary()
    }
  }, [fetchDictionary, clearDictionary])

  const handleSelectTable = async (schema: string, table: string) => {
    await fetchTable(schema, table)
  }

  const handleRefresh = async () => {
    setIsRefreshing(true)
    try {
      await refreshSchema()
    } finally {
      setIsRefreshing(false)
    }
  }

  // Loading state
  if (dictionaryStatus === "loading" && !dataDictionary) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <RefreshCw className="mx-auto h-8 w-8 animate-spin text-muted-foreground" />
          <p className="mt-4 text-sm text-muted-foreground">
            Loading data dictionary...
          </p>
        </div>
      </div>
    )
  }

  // Error state
  if (dictionaryStatus === "error") {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <Database className="mx-auto h-12 w-12 text-muted-foreground" />
          <h2 className="mt-4 text-lg font-medium">Failed to load dictionary</h2>
          <p className="mt-2 text-sm text-muted-foreground">
            There was an error loading the data dictionary
          </p>
          <Button onClick={() => fetchDictionary()} className="mt-4">
            Try Again
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full">
      {/* Left Panel - Schema Tree */}
      <div className="w-64 flex-shrink-0 border-r bg-muted/30" data-testid="schema-panel">
        <div className="flex items-center justify-between border-b p-3">
          <h2 className="text-sm font-medium">Tables</h2>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            onClick={handleRefresh}
            disabled={isRefreshing}
            data-testid="refresh-schema-btn"
          >
            <RefreshCw
              className={`h-4 w-4 ${isRefreshing ? "animate-spin" : ""}`}
            />
          </Button>
        </div>
        {dataDictionary && (
          <SchemaTree
            dictionary={dataDictionary}
            selectedTable={
              selectedTable
                ? { schema: selectedTable.schema, table: selectedTable.table }
                : null
            }
            onSelectTable={handleSelectTable}
          />
        )}
      </div>

      {/* Right Panel - Table Detail */}
      <div className="flex-1 overflow-hidden" data-testid="table-detail-panel">
        {selectedTable ? (
          <TableDetail
            key={`${selectedTable.schema}.${selectedTable.table}`}
            table={selectedTable}
          />
        ) : (
          <div className="flex h-full items-center justify-center">
            <div className="text-center">
              <Database className="mx-auto h-12 w-12 text-muted-foreground" />
              <h2 className="mt-4 text-lg font-medium">Select a table</h2>
              <p className="mt-2 text-sm text-muted-foreground">
                Choose a table from the left panel to view its details
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
