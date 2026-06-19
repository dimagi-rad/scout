import { useEffect, useState } from "react"

import {
  crossOppApi,
  type DashboardResponse,
  type InspectorResponse,
  type MeasureLineage,
} from "@/api/crossopp"
import { useAppStore } from "@/store/store"

const fmt = (v: string | number | null): string => {
  if (v == null) return "—"
  if (typeof v === "number") return Number.isInteger(v) ? v.toString() : v.toFixed(2)
  return v
}

// Strip Cube's "measure(cube.<name>)" wrapper to the bare measure name for column headers.
const measureLabel = (col: string): string =>
  col.replace(/^measure\([^.]+\./i, "").replace(/\)$/, "")

export function CrossOppPage() {
  const workspaceId = useAppStore((s) => s.activeDomainId)
  if (!workspaceId) {
    return <div className="p-6 text-sm text-muted-foreground">Select a workspace.</div>
  }
  // Key on workspaceId so switching workspaces remounts the dashboard, resetting
  // all per-workspace state (error, data, expanded measure) instead of leaving a
  // stale error banner or another workspace's data behind.
  return <CrossOppDashboard key={workspaceId} workspaceId={workspaceId} />
}

function CrossOppDashboard({ workspaceId }: { workspaceId: string }) {
  const [dash, setDash] = useState<DashboardResponse | null>(null)
  const [insp, setInsp] = useState<InspectorResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [openMeasure, setOpenMeasure] = useState<string | null>(null)
  const [showModel, setShowModel] = useState(false)

  useEffect(() => {
    let active = true
    const fail = (e: unknown) => {
      if (active) setError(e instanceof Error ? e.message : String(e))
    }
    crossOppApi
      .dashboard(workspaceId)
      .then((d) => {
        if (active) setDash(d)
      })
      .catch(fail)
    crossOppApi
      .inspector(workspaceId)
      .then((i) => {
        if (active) setInsp(i)
      })
      .catch(fail)
    return () => {
      active = false
    }
  }, [workspaceId])

  const measureCols = dash ? dash.columns.slice(1) : []

  return (
    <div className="space-y-6 p-6" data-testid="crossopp-page">
      <div>
        <h1 className="text-xl font-semibold">Cross-Opp Dashboard</h1>
        <p className="text-sm text-muted-foreground">
          Measures compared across opportunities — every number traceable to the field, label,
          and SQL it came from.
        </p>
      </div>

      {error && (
        <div className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {dash && (
        <div className="overflow-x-auto rounded border">
          <table className="w-full text-sm" data-testid="crossopp-dashboard-table">
            <thead className="bg-muted/50">
              <tr>
                <th className="px-3 py-2 text-left font-medium">opportunity</th>
                {measureCols.map((c) => (
                  <th key={c} className="px-3 py-2 text-right font-medium">
                    {measureLabel(c)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {dash.rows.map((row) => (
                <tr
                  key={String(row[0])}
                  className="border-t"
                  data-testid={`crossopp-row-${row[0]}`}
                >
                  <td className="px-3 py-2 font-mono">{String(row[0])}</td>
                  {row.slice(1).map((v, i) => (
                    <td key={i} className="px-3 py-2 text-right tabular-nums">
                      {fmt(v)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {insp && (
        <div className="space-y-3">
          <h2 className="text-lg font-semibold">How each number was computed</h2>
          <p className="text-sm text-muted-foreground">
            For every measure, this shows which field each opportunity's app resolved to (apps
            differ), the human label it matched on, the confidence, and the exact SQL.
          </p>
          {insp.measures.map((m) => (
            <MeasureInspector
              key={m.measure}
              measure={m}
              open={openMeasure === m.measure}
              onToggle={() =>
                setOpenMeasure(openMeasure === m.measure ? null : m.measure)
              }
            />
          ))}

          <button
            type="button"
            className="text-sm underline"
            onClick={() => setShowModel((s) => !s)}
            data-testid="crossopp-show-model"
          >
            {showModel ? "Hide" : "Show"} the generated Cube model — the exact SQL Cube runs
          </button>
          {showModel && (
            <pre
              className="overflow-x-auto rounded border bg-muted/30 p-3 text-xs"
              data-testid="crossopp-model-yaml"
            >
              {insp.model_yaml}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

function MeasureInspector({
  measure,
  open,
  onToggle,
}: {
  measure: MeasureLineage
  open: boolean
  onToggle: () => void
}) {
  const { coverage } = measure
  return (
    <div className="rounded border" data-testid={`crossopp-measure-${measure.measure}`}>
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center justify-between px-3 py-2 text-left hover:bg-muted/30"
      >
        <span className="font-medium">{measure.measure}</span>
        <span className="text-xs text-muted-foreground">
          {coverage.resolved}/{coverage.total} resolved
          {coverage.low_confidence ? `, ${coverage.low_confidence} low-confidence` : ""}
          {coverage.absent ? `, ${coverage.absent} absent` : ""}
        </span>
      </button>
      {open && (
        <div className="overflow-x-auto border-t">
          <table className="w-full text-xs">
            <thead className="bg-muted/40">
              <tr>
                <th className="px-2 py-1 text-left font-medium">opp</th>
                <th className="px-2 py-1 text-left font-medium">resolved field</th>
                <th className="px-2 py-1 text-left font-medium">label (the question)</th>
                <th className="px-2 py-1 text-right font-medium">conf</th>
                <th className="px-2 py-1 text-left font-medium">SQL expression</th>
              </tr>
            </thead>
            <tbody>
              {measure.opps.map((o) => (
                <tr key={o.opportunity_id} className="border-t align-top">
                  <td className="px-2 py-1 font-mono">{o.opportunity_id}</td>
                  <td className="px-2 py-1 font-mono">{o.column || "—"}</td>
                  <td className="px-2 py-1">
                    {o.matched_label ||
                      (o.status === "absent" ? (
                        <span className="text-amber-600">absent in this app</span>
                      ) : (
                        "—"
                      ))}
                  </td>
                  <td className="px-2 py-1 text-right tabular-nums">
                    {o.status === "absent" ? "—" : o.confidence.toFixed(2)}
                  </td>
                  <td className="px-2 py-1 font-mono text-muted-foreground">
                    {o.sql_expression || "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
