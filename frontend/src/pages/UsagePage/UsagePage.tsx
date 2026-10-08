import { useEffect, useState, type ReactNode } from "react"
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"
import { ApiError } from "@/api/client"
import { useAppStore } from "@/store/store"
import { usageApi, type Percentiles, type UsageDashboard } from "@/api/usage"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { formatBytes, formatCompact, formatMs } from "./format"

const WINDOWS = [7, 30, 90] as const
const SERIES = "var(--chart-1)"
const GRID = "var(--border)"
const AXIS = "var(--muted-foreground)"
const TOOLTIP = {
  contentStyle: {
    background: "var(--popover)",
    border: "1px solid var(--border)",
    borderRadius: 8,
    fontSize: 12,
  },
  itemStyle: { color: "var(--foreground)" },
  labelStyle: { color: "var(--muted-foreground)" },
}

type Status = "loading" | "loaded" | "forbidden" | "error"

function formatCount(value: number): string {
  return value.toLocaleString()
}

function percent(part: number, whole: number): string {
  return whole ? `${((part / whole) * 100).toFixed(1)}%` : "–"
}

function shortDay(day: string): string {
  return day.slice(5)
}

function Tile({ id, label, value, hint }: { id: string; label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-lg border bg-card p-4" data-testid={`usage-tile-${id}`}>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
      {hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
    </div>
  )
}

function Panel({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section className="rounded-lg border bg-card p-4" data-testid={`usage-panel-${id}`}>
      <h2 className="mb-3 text-sm font-medium">{title}</h2>
      {children}
    </section>
  )
}

function DailyBars({
  days,
  values,
  label,
}: {
  days: string[]
  values: (number | null)[]
  label: string
}) {
  const data = days.map((day, i) => ({ day: shortDay(day), [label]: values[i] ?? null }))
  return (
    <ResponsiveContainer width="100%" height={160}>
      <BarChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: -16 }}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="day" tick={{ fill: AXIS, fontSize: 11 }} tickLine={false} minTickGap={16} />
        <YAxis allowDecimals={false} tick={{ fill: AXIS, fontSize: 11 }} tickLine={false} axisLine={false} />
        <Tooltip {...TOOLTIP} cursor={{ fill: GRID, opacity: 0.4 }} />
        <Bar dataKey={label} fill={SERIES} radius={[4, 4, 0, 0]} maxBarSize={18} />
      </BarChart>
    </ResponsiveContainer>
  )
}

function DailyLine({
  days,
  values,
  label,
  format,
}: {
  days: string[]
  values: (number | null)[]
  label: string
  format?: (value: number) => string
}) {
  const data = days.map((day, i) => ({ day: shortDay(day), [label]: values[i] }))
  return (
    <ResponsiveContainer width="100%" height={180}>
      <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="day" tick={{ fill: AXIS, fontSize: 11 }} tickLine={false} minTickGap={16} />
        <YAxis
          tick={{ fill: AXIS, fontSize: 11 }}
          tickLine={false}
          axisLine={false}
          tickFormatter={format}
          allowDecimals={false}
          width={76}
        />
        <Tooltip
          {...TOOLTIP}
          formatter={(value) =>
            value === null || value === undefined
              ? "–"
              : format
                ? format(Number(value))
                : String(value)
          }
        />
        <Line
          type="linear"
          dataKey={label}
          stroke={SERIES}
          strokeWidth={2}
          dot={false}
        />
      </LineChart>
    </ResponsiveContainer>
  )
}

function LatencyChart({ data }: { data: UsageDashboard }) {
  const rows = data.days.map((day, i) => ({
    day: shortDay(day),
    "Median turn": data.turns.daily[i]?.duration_p50_ms ?? null,
    "95th percentile turn": data.turns.daily[i]?.duration_p95_ms ?? null,
  }))
  return (
    <ResponsiveContainer width="100%" height={200}>
      <LineChart data={rows} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="day" tick={{ fill: AXIS, fontSize: 11 }} tickLine={false} minTickGap={16} />
        <YAxis
          tick={{ fill: AXIS, fontSize: 11 }}
          tickLine={false}
          axisLine={false}
          tickFormatter={(v) => formatMs(Number(v))}
          width={76}
        />
        <Tooltip
          {...TOOLTIP}
          formatter={(value) => formatMs(value === null || value === undefined ? null : Number(value))}
        />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        {/* One hue; the dash tells the two percentiles apart without relying on colour. */}
        <Line
          type="linear"
          dataKey="Median turn"
          stroke={SERIES}
          strokeWidth={2}
          dot={false}
        />
        <Line
          type="linear"
          dataKey="95th percentile turn"
          stroke={SERIES}
          strokeWidth={2}
          strokeDasharray="5 4"
          strokeOpacity={0.7}
          dot={false}
        />
      </LineChart>
    </ResponsiveContainer>
  )
}

function SmallMultiples({
  id,
  days,
  series,
}: {
  id: string
  days: string[]
  series: { key: string; label: string; values: (number | null)[] }[]
}) {
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {series.map(({ key, label, values }) => (
        <div key={key} data-testid={`usage-${id}-${key}`}>
          <div className="mb-1 flex items-baseline justify-between text-xs">
            <span className="text-muted-foreground">{label}</span>
            <span className="tabular-nums">
              {values.every((value) => value === null)
                ? "–"
                : formatCount(values.reduce<number>((total, value) => total + (value ?? 0), 0))}
            </span>
          </div>
          <DailyBars days={days} values={values} label={label} />
        </div>
      ))}
    </div>
  )
}

function percentileText(p: Percentiles): string {
  return `${formatMs(p.p50)} / ${formatMs(p.p95)}`
}

function Dashboard({ data }: { data: UsageDashboard }) {
  const { turns } = data
  const failed = turns.outcomes.failed
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
        <Tile id="dau" label="Active users, 24 h" value={formatCount(data.active_users.dau)} />
        <Tile id="wau" label="Active users, 7 days" value={formatCount(data.active_users.wau)} />
        <Tile id="mau" label="Active users, 30 days" value={formatCount(data.active_users.mau)} />
        <Tile
          id="turns"
          label="Chat turns"
          value={formatCount(turns.total)}
          hint={`${percent(failed, turns.total)} failed · ${percent(turns.outcomes.stopped, turns.total)} stopped · ${formatCount(turns.background)} run by the worker`}
        />
        <Tile
          id="ttft"
          label="First token, median / p95"
          value={formatMs(turns.ttft_ms.p50)}
          hint={`p95 ${formatMs(turns.ttft_ms.p95)}`}
        />
        <Tile
          id="tokens"
          label="Chat model tokens, in / out, worker turns included"
          value={formatCompact(turns.tokens.input)}
          hint={`${formatCompact(turns.tokens.output)} out · ${formatCompact(turns.tokens.cache_read)} from cache`}
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Panel id="active-users" title="Active users per day">
          <DailyLine days={data.days} values={data.active_users.daily} label="Active users" />
        </Panel>
        <Panel id="turn-latency" title="Turn duration per day (all outcomes)">
          <LatencyChart data={data} />
        </Panel>
      </div>

      <Panel id="features" title="Feature use per day">
        <SmallMultiples
          id="feature"
          days={data.days}
          series={[
            { key: "turns", label: "Chat turns", values: data.features.turns },
            { key: "artifact-views", label: "Artifact views", values: data.features.artifact_views },
            { key: "recipe-runs", label: "Recipe runs", values: data.features.recipe_runs },
            {
              key: "workspace-switches",
              label: "Workspace switches",
              values: data.features.workspace_switches,
            },
            { key: "logins", label: "Logins", values: data.features.logins },
          ]}
        />
      </Panel>

      <Panel id="created" title="Created per day">
        <SmallMultiples
          id="created"
          days={data.days}
          series={[
            { key: "threads", label: "Threads", values: data.created.threads },
            { key: "artifacts", label: "Artifacts", values: data.created.artifacts },
            { key: "workspaces", label: "Workspaces", values: data.created.workspaces },
            { key: "tenants", label: "Tenants", values: data.created.tenants },
          ]}
        />
      </Panel>

      <Panel id="updated" title="Changed per day, new rows included (nightly snapshot; blank until it runs)">
        <SmallMultiples
          id="updated"
          days={data.days}
          series={[
            { key: "threads", label: "Threads", values: data.updated.threads },
            { key: "artifacts", label: "Artifacts", values: data.updated.artifacts },
            { key: "workspaces", label: "Workspaces", values: data.updated.workspaces },
            { key: "tenants", label: "Tenants", values: data.updated.tenants },
          ]}
        />
      </Panel>

      <Panel id="tools" title="Tool calls, all runs (chat, worker and recipes)">
        {data.tools.length === 0 ? (
          <p className="text-sm text-muted-foreground">No tool calls in this window.</p>
        ) : (
          <Table data-testid="usage-tools-table">
            <TableHeader>
              <TableRow>
                <TableHead>Tool</TableHead>
                <TableHead className="text-right">Calls</TableHead>
                <TableHead className="text-right">Error rate</TableHead>
                <TableHead className="text-right">Median</TableHead>
                <TableHead className="text-right">p95</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.tools.map((tool) => (
                <TableRow key={tool.name} data-testid={`usage-tool-${tool.name}`}>
                  <TableCell className="font-mono text-xs">{tool.name}</TableCell>
                  <TableCell className="text-right tabular-nums">{formatCount(tool.calls)}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {(tool.error_rate * 100).toFixed(1)}%
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{formatMs(tool.p50_ms)}</TableCell>
                  <TableCell className="text-right tabular-nums">{formatMs(tool.p95_ms)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        {turns.tool_calls_per_turn !== null && (
          <p className="mt-2 text-xs text-muted-foreground">
            {turns.tool_calls_per_turn} tool calls per turn on average.
          </p>
        )}
      </Panel>

      <div className="grid gap-6 lg:grid-cols-3">
        <Panel id="loads" title="Workspace loads">
          <dl className="space-y-1 text-sm">
            <div className="flex justify-between">
              <dt className="text-muted-foreground">Loads</dt>
              <dd className="tabular-nums">{formatCount(data.loads.total)}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-muted-foreground">Failed</dt>
              <dd className="tabular-nums">{percent(data.loads.failed, data.loads.total)}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-muted-foreground">Median / p95</dt>
              <dd className="tabular-nums">{percentileText(data.loads.duration_ms)}</dd>
            </div>
          </dl>
          {data.loads.phases.length > 0 && (
            <Table className="mt-3" data-testid="usage-load-phases">
              <TableHeader>
                <TableRow>
                  <TableHead>Phase</TableHead>
                  <TableHead className="text-right">Median</TableHead>
                  <TableHead className="text-right">p95</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.loads.phases.map((phase) => (
                  <TableRow key={phase.phase}>
                    <TableCell className="text-xs">{phase.phase.replaceAll("_", " ")}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatMs(phase.p50_ms)}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatMs(phase.p95_ms)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Panel>
        <Panel id="materializations" title="Tenant materializations">
          <dl className="space-y-1 text-sm">
            <div className="flex justify-between">
              <dt className="text-muted-foreground">Runs</dt>
              <dd className="tabular-nums">{formatCount(data.materializations.total)}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-muted-foreground">Completed median / p95</dt>
              <dd className="tabular-nums">{percentileText(data.materializations.duration_ms)}</dd>
            </div>
            {Object.entries(data.materializations.states).map(([state, count]) => (
              <div key={state} className="flex justify-between">
                <dt className="text-muted-foreground">{state}</dt>
                <dd className="tabular-nums">{percent(count, data.materializations.total)}</dd>
              </div>
            ))}
          </dl>
        </Panel>
        <Panel id="recipes" title="Recipe runs">
          <dl className="space-y-1 text-sm">
            <div className="flex justify-between">
              <dt className="text-muted-foreground">Runs</dt>
              <dd className="tabular-nums">{formatCount(data.recipe_runs.total)}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-muted-foreground">Failed</dt>
              <dd className="tabular-nums">{percent(data.recipe_runs.failed, data.recipe_runs.total)}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-muted-foreground">Completed median / p95</dt>
              <dd className="tabular-nums">{percentileText(data.recipe_runs.duration_ms)}</dd>
            </div>
          </dl>
        </Panel>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Panel id="tokens-by-workspace" title="Model tokens, top 20 workspaces, chat and recipes">
          {data.tokens_by_workspace.length === 0 ? (
            <p className="text-sm text-muted-foreground">No model usage in this window.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Workspace</TableHead>
                  <TableHead className="text-right">Runs</TableHead>
                  <TableHead className="text-right">In</TableHead>
                  <TableHead className="text-right">Out</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.tokens_by_workspace.map((row) => (
                  <TableRow key={row.workspace_id} data-testid={`usage-workspace-tokens-${row.workspace_id}`}>
                    <TableCell className="max-w-48 truncate">{row.name}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatCount(row.runs)}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatCount(row.input_tokens)}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatCount(row.output_tokens)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Panel>
        <Panel id="schema-sizes" title="Tenant data on disk">
          <DailyLine
            days={data.days}
            values={data.schema_sizes.total_daily}
            label="All tenant schemas"
            format={formatBytes}
          />
          {data.schema_sizes.latest_skipped && (
            <p className="mt-2 text-xs text-muted-foreground" data-testid="usage-schema-skipped">
              {data.schema_sizes.latest_skipped.schemas} schema(s) could not be measured on{" "}
              {data.schema_sizes.latest_skipped.day}; figures here are from the last complete night.
            </p>
          )}
          {data.schema_sizes.retained_bytes ? (
            <p className="mt-2 text-xs text-muted-foreground" data-testid="usage-schema-retained">
              {formatBytes(data.schema_sizes.retained_bytes)} of it is in schemas not serving
              queries (failed loads, teardowns).
            </p>
          ) : null}
          {data.schema_sizes.top_tenants.length > 0 && (
            <Table className="mt-3" data-testid="usage-schema-sizes-table">
              <TableHeader>
                <TableRow>
                  <TableHead>Largest tenants, {data.schema_sizes.as_of}</TableHead>
                  <TableHead className="text-right">Size</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.schema_sizes.top_tenants.map((row) => (
                  <TableRow key={row.tenant_id} data-testid={`usage-tenant-size-${row.tenant_id}`}>
                    <TableCell className="max-w-48 truncate">{row.name}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatBytes(row.bytes)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Panel>
      </div>
    </div>
  )
}

function NoAccess() {
  return (
    <p className="text-sm text-muted-foreground" data-testid="usage-forbidden">
      You don&apos;t have access to the usage dashboard.
    </p>
  )
}

export function UsagePage() {
  const allowed = useAppStore((state) => state.user?.can_view_usage_dashboard === true)
  if (!allowed) {
    // The API refuses anyone without the permission anyway; this skips the request.
    return (
      <div className="p-4 sm:p-6" data-testid="usage-page">
        <h1 className="mb-2 text-2xl font-bold">Usage</h1>
        <NoAccess />
      </div>
    )
  }
  return <UsageDashboardPage />
}

function UsageDashboardPage() {
  const [days, setDays] = useState<number>(30)
  const [data, setData] = useState<UsageDashboard | null>(null)
  const [status, setStatus] = useState<Status>("loading")
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    usageApi
      .dashboard(days, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return
        setData(result)
        setStatus("loaded")
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        setStatus(error instanceof ApiError && error.status === 403 ? "forbidden" : "error")
      })
    return () => controller.abort()
  }, [days, attempt])

  return (
    <div className="p-4 sm:p-6" data-testid="usage-page">
      <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Usage</h1>
          <p className="text-muted-foreground">How Scout is used and where it is slow. Days are UTC; the last one is today so far.</p>
        </div>
        <div className="flex gap-1" role="group" aria-label="Time range">
          {WINDOWS.map((option) => (
            <Button
              key={option}
              size="sm"
              variant={option === days ? "default" : "outline"}
              aria-pressed={option === days}
              onClick={() => {
                if (option === days) return
                setStatus("loading")
                setDays(option)
              }}
              data-testid={`usage-days-${option}`}
            >
              {option} days
            </Button>
          ))}
        </div>
      </div>
      {status === "forbidden" && (
        <NoAccess />
      )}
      {status === "error" && (
        <div className="flex items-center gap-3 text-sm" data-testid="usage-error">
          <span className="text-muted-foreground">The usage numbers could not be loaded.</span>
          <Button size="sm" variant="outline" onClick={() => {
              setStatus("loading")
              setAttempt((n) => n + 1)
            }} data-testid="usage-retry">
            Try again
          </Button>
        </div>
      )}
      {status === "loading" && !data && (
        <p className="text-sm text-muted-foreground" data-testid="usage-loading">
          Loading usage…
        </p>
      )}
      {data && status === "loading" && (
        <p className="mb-3 text-sm text-muted-foreground" role="status" data-testid="usage-refreshing">
          Loading {days} days…
        </p>
      )}
      {data && status !== "forbidden" && status !== "error" && (
        <div
          aria-busy={status === "loading"}
          className={status === "loading" ? "opacity-50 transition-opacity" : "transition-opacity"}
          data-testid="usage-dashboard"
        >
          <Dashboard data={data} />
        </div>
      )}
    </div>
  )
}
