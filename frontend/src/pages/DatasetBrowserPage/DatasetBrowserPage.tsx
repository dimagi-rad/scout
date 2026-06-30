import { useEffect, useMemo, useState } from "react"
import type { ReactNode } from "react"
import { Database, RefreshCw, Search, Sigma, Table2 } from "lucide-react"
import { useAppStore } from "@/store/store"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"
import type { SemanticDataset, SemanticField } from "@/store/datasetSlice"

export function DatasetBrowserPage() {
  const catalog = useAppStore((s) => s.datasetCatalog)
  const status = useAppStore((s) => s.datasetStatus)
  const error = useAppStore((s) => s.datasetError)
  const selectedDataset = useAppStore((s) => s.selectedDataset)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const { fetchDatasets, fetchDataset, clearDatasets } = useAppStore((s) => s.datasetActions)
  const [search, setSearch] = useState("")
  const [refreshing, setRefreshing] = useState(false)

  useEffect(() => {
    if (!activeDomainId) return
    void fetchDatasets()
    return () => clearDatasets()
  }, [activeDomainId, fetchDatasets, clearDatasets])

  const filteredDatasets = useMemo(() => {
    const datasets = catalog?.datasets ?? []
    const q = search.trim().toLowerCase()
    if (!q) return datasets
    return datasets.filter((dataset) =>
      [dataset.name, dataset.label, dataset.description, dataset.table_name]
        .filter(Boolean)
        .some((value) => value.toLowerCase().includes(q))
    )
  }, [catalog?.datasets, search])

  const refresh = async () => {
    setRefreshing(true)
    try {
      await fetchDatasets()
    } finally {
      setRefreshing(false)
    }
  }

  if (status === "loading" && !catalog) {
    return <CenteredState icon={<RefreshCw className="h-8 w-8 animate-spin" />} title="Loading datasets" />
  }

  if (status === "not_materialized") {
    return (
      <CenteredState
        icon={<Database className="h-12 w-12" />}
        title="No datasets yet"
        body="Start a chat to load workspace data and build the semantic catalog."
      />
    )
  }

  if (status === "error") {
    return (
      <CenteredState
        icon={<Database className="h-12 w-12" />}
        title="Failed to load datasets"
        body={error ?? "There was an error loading the semantic catalog."}
        action={<Button onClick={() => void fetchDatasets()}>Try Again</Button>}
      />
    )
  }

  return (
    <div className="flex h-full">
      <aside className="flex w-72 shrink-0 flex-col border-r bg-muted/20" data-testid="dataset-panel">
        <div className="flex h-12 items-center justify-between border-b px-3">
          <div>
            <h1 className="text-sm font-semibold">Datasets</h1>
            {catalog && (
              <p className="text-xs text-muted-foreground">
                Model v{catalog.model.version}
              </p>
            )}
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={refresh}
            disabled={refreshing}
            data-testid="refresh-datasets-btn"
          >
            <RefreshCw className={cn("h-4 w-4", refreshing && "animate-spin")} />
          </Button>
        </div>

        <div className="border-b p-3">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-2 h-4 w-4 text-muted-foreground" />
            <Input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search datasets"
              className="h-8 pl-8"
              data-testid="dataset-search"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-2">
          {filteredDatasets.length === 0 ? (
            <div className="px-2 py-8 text-center text-sm text-muted-foreground">
              {search ? "No datasets found" : "No datasets available"}
            </div>
          ) : (
            filteredDatasets.map((dataset) => (
              <button
                key={dataset.id}
                onClick={() => void fetchDataset(dataset.name)}
                className={cn(
                  "mb-1 flex w-full items-start gap-2 rounded-md px-2 py-2 text-left text-sm hover:bg-accent",
                  selectedDataset?.id === dataset.id && "bg-accent"
                )}
                data-testid={`dataset-item-${dataset.name}`}
              >
                <Table2 className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{dataset.label || dataset.name}</span>
                  <span className="block truncate text-xs text-muted-foreground">
                    {dataset.measures.length} measures · {dataset.dimensions.length + dataset.time_dimensions.length} dimensions
                  </span>
                </span>
              </button>
            ))
          )}
        </div>
      </aside>

      <main className="min-w-0 flex-1 overflow-y-auto">
        {selectedDataset ? (
          <DatasetDetail dataset={selectedDataset} />
        ) : (
          <CenteredState
            icon={<Database className="h-12 w-12" />}
            title="Select a dataset"
            body="Choose a semantic dataset to inspect measures, dimensions, and provenance."
          />
        )}
      </main>
    </div>
  )
}

function DatasetDetail({ dataset }: { dataset: SemanticDataset }) {
  return (
    <div className="mx-auto max-w-5xl p-6">
      <div className="mb-6 border-b pb-5">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline">{dataset.name}</Badge>
          <h2 className="text-2xl font-semibold">{dataset.label || dataset.name}</h2>
        </div>
        {dataset.description && (
          <p className="mt-2 max-w-3xl text-sm text-muted-foreground">{dataset.description}</p>
        )}
        <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted-foreground">
          <Badge variant="secondary">{dataset.table_name}</Badge>
          {dataset.row_count != null && (
            <Badge variant="secondary">
              {dataset.row_count.toLocaleString()} rows at last materialization
            </Badge>
          )}
        </div>
      </div>

      <div className="grid gap-6 xl:grid-cols-[1fr_20rem]">
        <div className="space-y-6">
          <FieldSection
            title="Measures"
            icon={<Sigma className="h-4 w-4" />}
            fields={dataset.measures}
          />
          <FieldSection
            title="Dimensions"
            icon={<Database className="h-4 w-4" />}
            fields={[...dataset.time_dimensions, ...dataset.dimensions]}
          />
        </div>

        <aside className="space-y-4">
          <div className="rounded-md border p-4">
            <h3 className="text-sm font-medium">Query Surface</h3>
            <p className="mt-2 text-sm text-muted-foreground">
              Use these member names with `semantic_query`.
            </p>
            <div className="mt-3 space-y-2 text-xs">
              <div>
                <span className="font-medium">Default count</span>
                <code className="mt-1 block rounded bg-muted px-2 py-1">{dataset.name}.count</code>
              </div>
              {dataset.time_dimensions[0] && (
                <div>
                  <span className="font-medium">Primary time dimension</span>
                  <code className="mt-1 block rounded bg-muted px-2 py-1">
                    {dataset.time_dimensions[0].member}
                  </code>
                </div>
              )}
            </div>
          </div>

          <div className="rounded-md border p-4">
            <h3 className="text-sm font-medium">Relationships</h3>
            {dataset.relationships.length === 0 ? (
              <p className="mt-2 text-sm text-muted-foreground">No relationships defined yet.</p>
            ) : (
              <div className="mt-3 space-y-2">
                {dataset.relationships.map((relationship) => (
                  <div key={relationship.id} className="text-sm">
                    <div className="font-medium">{relationship.name}</div>
                    <div className="text-muted-foreground">{relationship.relationship_type}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </aside>
      </div>
    </div>
  )
}

function FieldSection({
  title,
  icon,
  fields,
}: {
  title: string
  icon: ReactNode
  fields: SemanticField[]
}) {
  return (
    <section>
      <div className="mb-3 flex items-center gap-2">
        {icon}
        <h3 className="text-sm font-semibold">{title}</h3>
        <Badge variant="secondary">{fields.length}</Badge>
      </div>
      <div className="overflow-hidden rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-[32%]">Member</TableHead>
              <TableHead className="w-[20%]">Type</TableHead>
              <TableHead>Description</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {fields.length === 0 ? (
              <TableRow>
                <TableCell colSpan={3} className="py-8 text-center text-sm text-muted-foreground">
                  No {title.toLowerCase()} defined.
                </TableCell>
              </TableRow>
            ) : (
              fields.map((field) => (
                <TableRow key={field.id}>
                  <TableCell>
                    <code className="rounded bg-muted px-1.5 py-0.5 text-xs">{field.member}</code>
                    <div className="mt-1 text-sm font-medium">{field.label}</div>
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline">
                      {field.measure_type || field.data_type || field.type}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {field.description || "—"}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
    </section>
  )
}

function CenteredState({
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
    <div className="flex h-full items-center justify-center">
      <div className="text-center text-muted-foreground">
        <div className="mx-auto mb-4 flex justify-center">{icon}</div>
        <h2 className="text-lg font-medium text-foreground">{title}</h2>
        {body && <p className="mt-2 max-w-md text-sm">{body}</p>}
        {action && <div className="mt-4">{action}</div>}
      </div>
    </div>
  )
}
