import { useEffect, useMemo, useState } from "react"
import type { ReactNode } from "react"
import { Check, ChevronDown, Database, RefreshCw, Search, Sigma, Table2, X } from "lucide-react"
import { useAppStore } from "@/store/store"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
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
import { isCreatedDataset, isCreatedField } from "./datasetMeta"
import { filterDatasets } from "./datasetSearch"

function CreatedBadge({ testId }: { testId?: string }) {
  return (
    <Badge
      variant="outline"
      className="border-sky-200 bg-sky-50 text-sky-800"
      title="Created through the canvas"
      data-testid={testId}
    >
      Created
    </Badge>
  )
}

export function DatasetBrowserPage() {
  const catalog = useAppStore((s) => s.datasetCatalog)
  const status = useAppStore((s) => s.datasetStatus)
  const error = useAppStore((s) => s.datasetError)
  const selectedDataset = useAppStore((s) => s.selectedDataset)
  const selectedDatasetStatus = useAppStore((s) => s.selectedDatasetStatus)
  const selectedDatasetError = useAppStore((s) => s.selectedDatasetError)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const { fetchDatasets, fetchDataset, clearDatasets } = useAppStore((s) => s.datasetActions)
  const [search, setSearch] = useState("")
  const [datasetPickerOpen, setDatasetPickerOpen] = useState(false)
  const [refreshing, setRefreshing] = useState(false)

  useEffect(() => {
    if (!activeDomainId) return
    void fetchDatasets()
    return () => clearDatasets()
  }, [activeDomainId, fetchDatasets, clearDatasets])

  const filteredDatasets = useMemo(() => {
    return filterDatasets(catalog?.datasets ?? [], search)
  }, [catalog?.datasets, search])

  const visibleDataset = selectedDataset
  const isDatasetLoading = selectedDatasetStatus === "loading"

  const handleDatasetPickerOpenChange = (open: boolean) => {
    setDatasetPickerOpen(open)
    if (!open) setSearch("")
  }

  const selectDataset = (dataset: SemanticDataset, resetSearch = true) => {
    setDatasetPickerOpen(false)
    if (resetSearch) setSearch("")
    if (selectedDataset?.id !== dataset.id || selectedDatasetStatus === "error") {
      void fetchDataset(dataset.name)
    }
  }

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
    <div className="flex h-full min-w-0 overflow-hidden">
      <aside
        className="hidden w-72 shrink-0 flex-col border-r bg-muted/20 lg:flex"
        data-testid="dataset-panel"
      >
        <div className="flex h-12 items-center justify-between border-b px-3">
          <div className="min-w-0">
            <h1 className="truncate text-sm font-semibold">Datasets</h1>
            {catalog && (
              <p className="truncate text-xs text-muted-foreground">
                {catalog.datasets.length} dataset{catalog.datasets.length === 1 ? "" : "s"}
              </p>
            )}
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={refresh}
            disabled={refreshing}
            aria-label="Refresh datasets"
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
              className="h-8 pl-8 pr-8"
              data-testid="dataset-search"
            />
            {search && (
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                className="absolute right-1 top-1 h-6 w-6"
                onClick={() => setSearch("")}
                aria-label="Clear dataset search"
                data-testid="clear-dataset-search"
              >
                <X className="h-3.5 w-3.5" />
              </Button>
            )}
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
                type="button"
                onClick={() => selectDataset(dataset, false)}
                className={cn(
                  "mb-1 flex w-full items-start gap-2 rounded-md px-2 py-2 text-left text-sm hover:bg-accent",
                  visibleDataset?.id === dataset.id && "bg-accent"
                )}
                data-testid={`dataset-item-${dataset.name}`}
              >
                <Table2 className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1">
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate font-medium">{dataset.label || dataset.name}</span>
                    {isCreatedDataset(dataset) && (
                      <CreatedBadge testId={`dataset-created-badge-${dataset.name}`} />
                    )}
                  </span>
                  <span className="block truncate text-xs text-muted-foreground">
                    {dataset.measures.length} measures ·{" "}
                    {dataset.dimensions.length + dataset.time_dimensions.length} dimensions
                  </span>
                </span>
              </button>
            ))
          )}
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="shrink-0 border-b bg-background lg:hidden">
          <div className="mx-auto flex w-full max-w-5xl flex-col gap-3 px-4 py-3 sm:px-6">
            <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
              <div className="min-w-0">
                <h1 className="text-xl font-semibold tracking-tight">Datasets</h1>
                <p className="mt-1 text-sm text-muted-foreground">
                  {catalog
                    ? `${catalog.datasets.length} dataset${catalog.datasets.length === 1 ? "" : "s"}`
                    : "Browse semantic datasets, measures, and dimensions."}
                </p>
              </div>
              <div className="flex w-full flex-col gap-2 sm:flex-row sm:items-center md:w-auto">
                <DatasetPicker
                  datasets={catalog?.datasets ?? []}
                  filteredDatasets={filteredDatasets}
                  selectedDataset={visibleDataset}
                  search={search}
                  open={datasetPickerOpen}
                  onOpenChange={handleDatasetPickerOpenChange}
                  onSearchChange={setSearch}
                  onClearSearch={() => setSearch("")}
                  onSelectDataset={(dataset) => selectDataset(dataset)}
                />
                <Button
                  variant="outline"
                  size="icon"
                  className="h-11 w-full sm:w-11"
                  onClick={refresh}
                  disabled={refreshing}
                  aria-label="Refresh datasets"
                  data-testid="mobile-refresh-datasets-btn"
                >
                  <RefreshCw className={cn("h-4 w-4", refreshing && "animate-spin")} />
                </Button>
              </div>
            </div>
          </div>
        </header>

        <main className="min-h-0 min-w-0 flex-1 overflow-y-auto">
          {isDatasetLoading ? (
            <CenteredState
              icon={<RefreshCw className="h-8 w-8 animate-spin" />}
              title={`Loading ${visibleDataset?.label || "dataset"}`}
              body="Fetching the selected dataset details."
            />
          ) : visibleDataset ? (
            <DatasetDetail
              dataset={visibleDataset}
              error={selectedDatasetStatus === "error" ? selectedDatasetError : null}
              onOpenDataset={(name) => {
                const target = catalog?.datasets.find((entry) => entry.name === name)
                if (target) {
                  selectDataset(target)
                } else {
                  void fetchDataset(name)
                }
              }}
            />
          ) : (
            <CenteredState
              icon={<Database className="h-12 w-12" />}
              title="Select a dataset"
              body="Choose a semantic dataset to inspect measures, dimensions, and provenance."
            />
          )}
        </main>
      </div>
    </div>
  )
}

function DatasetPicker({
  datasets,
  filteredDatasets,
  selectedDataset,
  search,
  open,
  onOpenChange,
  onSearchChange,
  onClearSearch,
  onSelectDataset,
}: {
  datasets: SemanticDataset[]
  filteredDatasets: SemanticDataset[]
  selectedDataset: SemanticDataset | null
  search: string
  open: boolean
  onOpenChange: (open: boolean) => void
  onSearchChange: (search: string) => void
  onClearSearch: () => void
  onSelectDataset: (dataset: SemanticDataset) => void
}) {
  const selectedLabel = selectedDataset?.label || selectedDataset?.name || "Select a dataset"
  const selectedMeta = selectedDataset
    ? `${selectedDataset.measures.length} measures · ${selectedDataset.dimensions.length + selectedDataset.time_dimensions.length} dimensions`
    : `${datasets.length} dataset${datasets.length === 1 ? "" : "s"}`

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          className="h-11 w-full justify-between gap-3 px-3 py-2 text-left sm:w-[22rem]"
          disabled={datasets.length === 0}
          data-testid="dataset-picker-trigger"
        >
          <span className="flex min-w-0 items-center gap-2">
            <Table2 className="h-4 w-4 shrink-0 text-muted-foreground" />
            <span className="min-w-0">
              <span className="block truncate text-sm font-medium">{selectedLabel}</span>
              <span className="block truncate text-xs font-normal text-muted-foreground">
                {selectedMeta}
              </span>
            </span>
          </span>
          <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="w-[calc(100vw-2rem)] max-w-[28rem] p-0"
        data-testid="dataset-picker-popover"
      >
        <div className="border-b p-2">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-2 h-4 w-4 text-muted-foreground" />
            <Input
              autoFocus
              value={search}
              onChange={(event) => onSearchChange(event.target.value)}
              placeholder="Search datasets"
              className="h-8 pl-8 pr-8"
              data-testid="dataset-picker-search"
            />
            {search && (
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                className="absolute right-1 top-1 h-6 w-6"
                onClick={onClearSearch}
                aria-label="Clear dataset search"
                data-testid="clear-dataset-picker-search"
              >
                <X className="h-3.5 w-3.5" />
              </Button>
            )}
          </div>
        </div>
        <div className="max-h-72 overflow-y-auto p-1">
          {filteredDatasets.length === 0 ? (
            <div className="px-3 py-8 text-center text-sm text-muted-foreground">
              {search ? "No datasets found" : "No datasets available"}
            </div>
          ) : (
            filteredDatasets.map((dataset) => {
              const isSelected = selectedDataset?.id === dataset.id
              return (
                <button
                  key={dataset.id}
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  onClick={() => onSelectDataset(dataset)}
                  className={cn(
                    "flex w-full items-start gap-2 rounded-md px-2 py-2 text-left text-sm outline-none hover:bg-accent focus:bg-accent",
                    isSelected && "bg-accent"
                  )}
                  data-testid={`dataset-picker-item-${dataset.name}`}
                >
                  <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center">
                    {isSelected ? (
                      <Check className="h-4 w-4 text-primary" />
                    ) : (
                      <Table2 className="h-4 w-4 text-muted-foreground" />
                    )}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{dataset.label || dataset.name}</span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {dataset.measures.length} measures · {dataset.dimensions.length + dataset.time_dimensions.length} dimensions
                    </span>
                  </span>
                </button>
              )
            })
          )}
        </div>
      </PopoverContent>
    </Popover>
  )
}

function DatasetDetail({
  dataset,
  error,
  onOpenDataset,
}: {
  dataset: SemanticDataset
  error: string | null
  onOpenDataset: (name: string) => void
}) {
  const displayName = dataset.label || dataset.name
  const showTableName = dataset.table_name && dataset.table_name !== dataset.name

  return (
    <div className="mx-auto max-w-5xl p-4 sm:p-6">
      <div className="mb-6 border-b pb-5">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <h2 className="min-w-0 break-words text-xl font-semibold sm:text-2xl">
            {displayName}
          </h2>
          {isCreatedDataset(dataset) && (
            <CreatedBadge testId={`dataset-detail-created-badge-${dataset.name}`} />
          )}
        </div>
        {error && (
          <p className="mt-2 max-w-3xl text-sm text-destructive">
            Could not refresh dataset details: {error}
          </p>
        )}
        {dataset.description && (
          <p className="mt-2 max-w-3xl text-sm text-muted-foreground">{dataset.description}</p>
        )}
        <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted-foreground">
          {showTableName && (
            <Badge variant="secondary" className="max-w-full truncate">
              Table {dataset.table_name}
            </Badge>
          )}
          {dataset.primary_key && (
            <Badge variant="secondary" data-testid="dataset-primary-key-badge">
              PK {dataset.primary_key}
            </Badge>
          )}
          {dataset.row_count != null && (
            <Badge
              variant="secondary"
              title="Recorded by the most recent data load, not a live count query."
            >
              {dataset.row_count.toLocaleString()} rows from last data load
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
            <h3 className="text-sm font-medium">Relationships</h3>
            {dataset.relationships.length === 0 ? (
              <p className="mt-2 text-sm text-muted-foreground">No relationships defined yet.</p>
            ) : (
              <div className="mt-3 space-y-3" data-testid="dataset-relationships">
                {dataset.relationships.map((relationship) => {
                  const outgoing = relationship.direction !== "incoming"
                  const other = outgoing ? relationship.to_dataset : relationship.from_dataset
                  return (
                    <div key={`${relationship.id}-${relationship.direction}`} className="text-sm">
                      <div className="flex min-w-0 items-center gap-1.5 font-medium">
                        <span className="text-muted-foreground">{outgoing ? "→" : "←"}</span>
                        <button
                          type="button"
                          className="truncate text-left text-primary hover:underline"
                          onClick={() => onOpenDataset(other)}
                          data-testid={`relationship-target-${other}`}
                        >
                          {other}
                        </button>
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {relationship.relationship_type}
                        {outgoing ? "" : " (referenced by)"}
                      </div>
                      {relationship.join_expression && (
                        <code className="mt-1 block truncate rounded bg-muted px-2 py-1 text-[11px]">
                          {relationship.join_expression}
                        </code>
                      )}
                    </div>
                  )
                })}
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
        <Table className="min-w-[42rem]">
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
                    <span className="flex flex-wrap items-center gap-1.5">
                      <code className="rounded bg-muted px-1.5 py-0.5 text-xs">{field.member}</code>
                      {isCreatedField(field) && (
                        <CreatedBadge testId={`field-created-badge-${field.name}`} />
                      )}
                    </span>
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
