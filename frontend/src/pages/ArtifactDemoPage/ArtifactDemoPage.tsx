import { useEffect, useMemo, useRef, useState } from "react"
import { Link } from "react-router-dom"
import {
  AlertCircle,
  ArrowLeft,
  Braces,
  CheckCircle2,
  CircleDashed,
  FileCode2,
  FileText,
  Layers3,
  Loader2,
  RefreshCw,
  Shapes,
} from "lucide-react"

import { ArtifactGraphRenderer } from "@/components/ArtifactGraph"
import { RechartsFrame, type RechartsNode } from "@/components/ArtifactGraph/recharts"
import {
  ArtifactActions,
  ArtifactDataDialog,
  type QueryDataResponse,
} from "@/components/ArtifactViewer"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"

import {
  chartTrees,
  demoQueryData,
  regionRows,
  scatterRows,
  statusRows,
  storyArtifact,
  trendRows,
  workflowRows,
} from "./demoData"

const DEMO_WORKSPACE_ID = "artifact-demo-workspace"

const chartSamples: Array<{
  id: keyof typeof chartTrees
  title: string
  description: string
  rows: Array<Record<string, unknown>>
}> = [
  {
    id: "line",
    title: "Trend over time",
    description: "Compare series and inspect exact values with a shared tooltip.",
    rows: trendRows,
  },
  {
    id: "bar",
    title: "Stacked composition",
    description: "Show how a total breaks down across regions or categories.",
    rows: regionRows,
  },
  {
    id: "area",
    title: "Volume and shape",
    description: "Emphasize magnitude while keeping the time pattern legible.",
    rows: trendRows,
  },
  {
    id: "pie",
    title: "Part-to-whole",
    description: "Use a compact donut for a small, clearly distinct set of categories.",
    rows: statusRows,
  },
  {
    id: "scatter",
    title: "Relationship",
    description: "Explore the relationship between workload and visit duration.",
    rows: scatterRows,
  },
  {
    id: "composed",
    title: "Measure and target",
    description: "Combine volume, rate, a second axis, and a reference line.",
    rows: workflowRows,
  },
]

const formatRows = [
  ["Story", "Narrative blocks, semantic queries, tables, KPIs, and Recharts", "Recommended for analysis"],
  ["React", "Interactive custom interfaces rendered in the artifact sandbox", "Custom experiences"],
  ["HTML", "Self-contained documents and lightweight interactive layouts", "Portable layouts"],
  ["Markdown", "Readable briefs, findings, and documentation", "Narrative output"],
  ["SVG", "Crisp diagrams, maps, and scalable vector output", "Visual explainers"],
] as const

const blockGroups = [
  ["Narrative", "Title, section, question, summary, Markdown"],
  ["Controls", "Date filter and period selector"],
  ["Data", "Semantic query, literal rows, typed references"],
  ["Visual", "Stat, graph, table, responsive row groups"],
] as const

export function ArtifactDemoPage() {
  const [dataOpen, setDataOpen] = useState(false)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [refreshCount, setRefreshCount] = useState(0)
  const refreshTimerRef = useRef<number | null>(null)
  const queryData = useMemo<QueryDataResponse>(() => demoQueryData(refreshCount), [refreshCount])

  useEffect(() => () => {
    if (refreshTimerRef.current !== null) {
      window.clearTimeout(refreshTimerRef.current)
    }
  }, [])

  function handleRefresh() {
    if (isRefreshing) return
    setIsRefreshing(true)
    refreshTimerRef.current = window.setTimeout(() => {
      setRefreshCount((count) => count + 1)
      setIsRefreshing(false)
      refreshTimerRef.current = null
    }, 450)
  }

  return (
    <div className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6 lg:px-8" data-testid="artifact-demo-page">
      <Button variant="ghost" size="sm" asChild className="mb-5 -ml-2">
        <Link to="/artifacts">
          <ArrowLeft aria-hidden="true" />
          Artifacts
        </Link>
      </Button>

      <header className="flex flex-col gap-5 border-b border-border pb-6 lg:flex-row lg:items-end lg:justify-between">
        <div className="max-w-3xl space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary">Demo data</Badge>
            <span className="text-xs text-muted-foreground">Story artifact · version 4</span>
          </div>
          <div>
            <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">Artifact showcase</h1>
            <p className="mt-2 max-w-[68ch] text-sm leading-6 text-muted-foreground sm:text-base">
              Explore the production artifact renderer, the supported Recharts patterns, and the
              data contract behind a reusable analysis. Every value on this page is synthetic.
            </p>
          </div>
        </div>
        <ArtifactActions
          onViewData={() => setDataOpen(true)}
          onExportPdf={() => window.print()}
        />
      </header>

      <dl className="grid border-b border-border sm:grid-cols-3 sm:divide-x sm:divide-border">
        <SummaryFact term="Canonical format" value="Story block graph" />
        <SummaryFact term="Chart engine" value="Recharts" />
        <SummaryFact term="Data contract" value="Queries, results, and manifest" />
      </dl>

      <Tabs defaultValue="artifact" className="mt-8">
        <TabsList className="grid h-auto w-full grid-cols-3 sm:flex sm:w-fit" aria-label="Artifact showcase sections">
          <TabsTrigger value="artifact" className="min-w-0 whitespace-normal px-2 py-2 text-center leading-tight">
            Full artifact
          </TabsTrigger>
          <TabsTrigger value="charts" className="min-w-0 whitespace-normal px-2 py-2 text-center leading-tight">
            Chart gallery
          </TabsTrigger>
          <TabsTrigger value="system" className="min-w-0 whitespace-normal px-2 py-2 text-center leading-tight">
            States & formats
          </TabsTrigger>
        </TabsList>

        <TabsContent value="artifact" className="mt-6">
          <section aria-labelledby="full-artifact-heading">
            <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
              <div>
                <h2 id="full-artifact-heading" className="text-xl font-semibold">A complete Story artifact</h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  Narrative, KPIs, charts, a responsive chart row, a table, and provenance render through the production block engine.
                </p>
              </div>
              <span className="text-xs text-muted-foreground">Resize the window to test the layout</span>
            </div>
            <div className="min-h-[48rem] overflow-hidden rounded-xl border border-border bg-background">
              <ArtifactGraphRenderer artifact={storyArtifact} workspaceId={DEMO_WORKSPACE_ID} />
            </div>
          </section>
        </TabsContent>

        <TabsContent value="charts" className="mt-6">
          <ChartGallery />
        </TabsContent>

        <TabsContent value="system" className="mt-6">
          <SystemReference onViewData={() => setDataOpen(true)} />
        </TabsContent>
      </Tabs>

      <ArtifactDataDialog
        open={dataOpen}
        onOpenChange={setDataOpen}
        artifactTitle="Community visit operations review · synthetic demo"
        queryData={queryData}
        isLoading={isRefreshing}
        error={null}
        onRefresh={handleRefresh}
      />
    </div>
  )
}

function SummaryFact({ term, value }: { term: string; value: string }) {
  return (
    <div className="py-4 sm:px-5 sm:first:pl-0 sm:last:pr-0">
      <dt className="text-xs font-medium text-muted-foreground">{term}</dt>
      <dd className="mt-1 text-sm font-medium">{value}</dd>
    </div>
  )
}

function ChartGallery() {
  const [windowSize, setWindowSize] = useState<4 | 8 | 12>(12)
  const visibleTrendRows = trendRows.slice(-windowSize)

  return (
    <section aria-labelledby="chart-gallery-heading">
      <div className="flex flex-col gap-4 border-b border-border pb-5 md:flex-row md:items-end md:justify-between">
        <div>
          <h2 id="chart-gallery-heading" className="text-xl font-semibold">Recharts pattern gallery</h2>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            These are built with the same allow-listed Recharts tree used by Story artifacts. Hover a chart for exact values.
          </p>
        </div>
        <fieldset className="flex items-center gap-1" aria-label="Trend display window">
          <legend className="sr-only">Trend display window</legend>
          {([4, 8, 12] as const).map((size) => (
            <Button
              key={size}
              type="button"
              size="sm"
              variant={windowSize === size ? "secondary" : "ghost"}
              aria-pressed={windowSize === size}
              onClick={() => setWindowSize(size)}
            >
              {size} weeks
            </Button>
          ))}
        </fieldset>
      </div>

      <div className="grid gap-x-8 lg:grid-cols-2">
        {chartSamples.map((sample) => {
          const rows = sample.id === "line" || sample.id === "area"
            ? visibleTrendRows
            : sample.rows
          return (
            <ChartSample
              key={sample.id}
              title={sample.title}
              description={sample.description}
              rows={rows}
              tree={chartTrees[sample.id]}
            />
          )
        })}
      </div>
    </section>
  )
}

function ChartSample({
  title,
  description,
  rows,
  tree,
}: {
  title: string
  description: string
  rows: Array<Record<string, unknown>>
  tree: RechartsNode
}) {
  return (
    <section className="min-w-0 border-b border-border py-6">
      <h3 className="text-base font-semibold">{title}</h3>
      <p className="mt-1 min-h-10 text-sm leading-5 text-muted-foreground">{description}</p>
      <div className="mt-4" aria-label={`${title} chart`}>
        <RechartsFrame rows={rows} tree={tree} height={270} />
      </div>
    </section>
  )
}

function SystemReference({ onViewData }: { onViewData: () => void }) {
  return (
    <div className="space-y-10">
      <section aria-labelledby="states-heading">
        <div className="max-w-2xl">
          <h2 id="states-heading" className="text-xl font-semibold">Data states</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            The renderer names what is happening and gives the user a useful recovery when a query cannot complete.
          </p>
        </div>
        <div className="mt-5 divide-y divide-border border-y border-border">
          <StateRow
            icon={Loader2}
            iconClassName="animate-spin"
            title="Loading"
            description="Executing semantic queries and retaining the artifact layout while results arrive."
            sample="Loading data…"
          />
          <StateRow
            icon={AlertCircle}
            iconClassName="text-destructive"
            title="Query error"
            description="Names the failed data dependency so the artifact can be corrected or retried."
            sample="Data failed to load"
            sampleClassName="text-destructive"
          />
          <StateRow
            icon={CircleDashed}
            title="No results"
            description="Keeps the requested structure visible and distinguishes an empty result from a failure."
            sample="No data"
          />
          <StateRow
            icon={CheckCircle2}
            iconClassName="text-emerald-700 dark:text-emerald-400"
            title="Ready"
            description="Charts, tables, and formatted measures render from the same typed output rows."
            sample="Rows validated"
          />
        </div>
      </section>

      <section aria-labelledby="formats-heading">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div className="max-w-2xl">
            <h2 id="formats-heading" className="text-xl font-semibold">Artifact formats</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Story is the default analytical format. Sandboxed formats remain available when the output needs a custom document or interface.
            </p>
          </div>
          <Button type="button" variant="outline" size="sm" onClick={onViewData}>
            <Braces aria-hidden="true" />
            Inspect demo data
          </Button>
        </div>
        <div className="mt-5 overflow-hidden rounded-lg border border-border">
          <Table>
            <TableHeader>
              <TableRow className="bg-muted/50 hover:bg-muted/50">
                <TableHead>Format</TableHead>
                <TableHead>What it supports</TableHead>
                <TableHead>Best for</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {formatRows.map(([format, supports, bestFor]) => (
                <TableRow key={format}>
                  <TableCell className="font-medium">{format}</TableCell>
                  <TableCell className="min-w-72 whitespace-normal text-muted-foreground">{supports}</TableCell>
                  <TableCell className="text-muted-foreground">{bestFor}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </section>

      <section className="grid gap-8 lg:grid-cols-[1fr_1.15fr]" aria-labelledby="blocks-heading">
        <div>
          <h2 id="blocks-heading" className="text-xl font-semibold">Story block system</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Blocks bind outputs by typed references, then compose into responsive rows without custom page code.
          </p>
          <dl className="mt-5 divide-y divide-border border-y border-border">
            {blockGroups.map(([term, value]) => (
              <div key={term} className="grid gap-1 py-3 sm:grid-cols-[7rem_1fr]">
                <dt className="text-sm font-medium">{term}</dt>
                <dd className="text-sm text-muted-foreground">{value}</dd>
              </div>
            ))}
          </dl>
        </div>

        <div className="rounded-xl bg-muted/50 p-5 sm:p-6">
          <div className="flex items-center gap-2">
            <Layers3 className="h-4 w-4" aria-hidden="true" />
            <h3 className="font-semibold">What the artifact retains</h3>
          </div>
          <dl className="mt-5 grid gap-4 sm:grid-cols-2">
            <RetentionFact icon={Shapes} term="Structure" value="Block graph and responsive grouping" />
            <RetentionFact icon={Braces} term="Query contract" value="Measures, dimensions, and filters" />
            <RetentionFact icon={FileText} term="Results" value="Columns, rows, and truncation state" />
            <RetentionFact icon={FileCode2} term="Revision" value="Artifact version and generation manifest" />
          </dl>
          <Button type="button" variant="default" size="sm" className="mt-6" onClick={onViewData}>
            <RefreshCw aria-hidden="true" />
            Open query results
          </Button>
        </div>
      </section>
    </div>
  )
}

function StateRow({
  icon: Icon,
  iconClassName,
  title,
  description,
  sample,
  sampleClassName,
}: {
  icon: typeof Loader2
  iconClassName?: string
  title: string
  description: string
  sample: string
  sampleClassName?: string
}) {
  return (
    <div className="grid gap-3 py-4 sm:grid-cols-[1.1fr_1.8fr_0.8fr] sm:items-center">
      <div className="flex items-center gap-3">
        <Icon className={cn("h-4 w-4 text-muted-foreground", iconClassName)} aria-hidden="true" />
        <h3 className="text-sm font-medium">{title}</h3>
      </div>
      <p className="text-sm leading-5 text-muted-foreground">{description}</p>
      <code className={cn("w-fit rounded bg-muted px-2 py-1 text-xs", sampleClassName)}>{sample}</code>
    </div>
  )
}

function RetentionFact({
  icon: Icon,
  term,
  value,
}: {
  icon: typeof Shapes
  term: string
  value: string
}) {
  return (
    <div>
      <dt className="flex items-center gap-2 text-sm font-medium">
        <Icon className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
        {term}
      </dt>
      <dd className="mt-1 pl-6 text-sm leading-5 text-muted-foreground">{value}</dd>
    </div>
  )
}
