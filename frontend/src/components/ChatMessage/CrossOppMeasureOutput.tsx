import { useState } from "react"
import { SqlHighlighter } from "./SqlHighlighter"
import { approveMeasure } from "@/api/crossopp"

type Lineage = {
  opportunity_id: string
  status: string
  confidence: number
  column: string | null
  matched_label: string
  sql_expression: string | null
}

type Flagged = {
  opp_id: string
  guess: string | null
  confidence: number
  shortlist: { column: string; label: string; type: string }[]
}

export type MeasureOutput =
  | { status: "committed"; measure: string; lineage: Lineage[] }
  | {
      status: "needs_approval"
      draft_id: string
      measure: string
      flagged: Flagged[]
      resolved: { opp_id: string; column: string | null; confidence: number }[]
    }
  | { status: "exists"; measure: string; message?: string }

export function CrossOppMeasureOutput({
  workspaceId,
  output,
}: {
  workspaceId: string
  output: MeasureOutput
}) {
  if (output.status === "exists") {
    return (
      <div className="text-xs text-muted-foreground">
        Measure &ldquo;{output.measure}&rdquo; already defined.
      </div>
    )
  }
  if (output.status === "committed") {
    return (
      <LineageTable
        measure={output.measure}
        rows={output.lineage}
        testid={`crossopp-measure-output-${output.measure}`}
      />
    )
  }
  return <ApprovalCard workspaceId={workspaceId} output={output} />
}

function LineageTable({
  measure,
  rows,
  testid,
}: {
  measure: string
  rows: Lineage[]
  testid: string
}) {
  return (
    <div data-testid={testid} className="space-y-2">
      <div className="text-xs font-medium">{measure} — per-opportunity mapping</div>
      <div className="overflow-x-auto rounded border border-border/50">
        <table className="w-full text-xs">
          <thead>
            <tr className="bg-muted/40">
              <th className="px-2 py-1 text-left">opp</th>
              <th className="px-2 py-1 text-left">field</th>
              <th className="px-2 py-1 text-left">label</th>
              <th className="px-2 py-1 text-right">conf</th>
              <th className="px-2 py-1 text-left">SQL</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.opportunity_id} className="border-t align-top">
                <td className="px-2 py-1 font-mono">{r.opportunity_id}</td>
                <td className="px-2 py-1 font-mono">{r.column ?? "—"}</td>
                <td className="px-2 py-1">{r.matched_label || "—"}</td>
                <td className="px-2 py-1 text-right tabular-nums">
                  {r.status === "absent" ? "—" : r.confidence.toFixed(2)}
                </td>
                <td className="px-2 py-1 font-mono text-muted-foreground">
                  {r.sql_expression ? <SqlHighlighter sql={r.sql_expression} /> : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function ApprovalCard({
  workspaceId,
  output,
}: {
  workspaceId: string
  output: Extract<MeasureOutput, { status: "needs_approval" }>
}) {
  const [choices, setChoices] = useState<
    Record<string, { action: "confirm" | "pick" | "reject"; column?: string }>
  >({})
  const [done, setDone] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const submit = async () => {
    try {
      const r = await approveMeasure(workspaceId, output.draft_id, choices)
      setDone(r.measure)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }

  if (done) {
    return (
      <div
        data-testid={`crossopp-approved-${output.draft_id}`}
        className="text-xs text-emerald-600"
      >
        Defined &ldquo;{done}&rdquo;. Ask your question again to see it.
      </div>
    )
  }

  return (
    <div
      data-testid={`crossopp-approval-${output.draft_id}`}
      className="space-y-2 rounded border border-amber-300 bg-amber-50/50 p-2"
    >
      <div className="text-xs font-medium">
        &ldquo;{output.measure}&rdquo; needs your confirmation on {output.flagged.length} opp(s)
      </div>
      {output.flagged.map((f) => (
        <div key={f.opp_id} className="rounded border px-2 py-1.5 text-xs space-y-1">
          <div className="font-mono">
            {f.opp_id}{" "}
            <span className="text-muted-foreground">
              (guess: {f.guess ?? "absent"}, conf {f.confidence.toFixed(2)})
            </span>
          </div>
          <div className="flex gap-1 flex-wrap items-center">
            <button
              type="button"
              data-testid={`crossopp-approve-confirm-${f.opp_id}`}
              onClick={() => setChoices((c) => ({ ...c, [f.opp_id]: { action: "confirm" } }))}
              className="rounded border px-1.5"
            >
              Confirm
            </button>
            <select
              data-testid={`crossopp-approve-pick-${f.opp_id}`}
              onChange={(e) =>
                setChoices((c) => ({
                  ...c,
                  [f.opp_id]: { action: "pick", column: e.target.value },
                }))
              }
              className="rounded border px-1"
            >
              <option value="">pick field…</option>
              {f.shortlist.map((s) => (
                <option key={s.column} value={s.column}>
                  {s.column} — {s.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              data-testid={`crossopp-approve-reject-${f.opp_id}`}
              onClick={() => setChoices((c) => ({ ...c, [f.opp_id]: { action: "reject" } }))}
              className="rounded border px-1.5"
            >
              Reject
            </button>
            {choices[f.opp_id] && (
              <span className="text-emerald-600">
                ✓ {choices[f.opp_id].action}
                {choices[f.opp_id].column ? `: ${choices[f.opp_id].column}` : ""}
              </span>
            )}
          </div>
        </div>
      ))}
      {err && <div className="text-red-600">{err}</div>}
      <button
        type="button"
        data-testid={`crossopp-approve-submit-${output.draft_id}`}
        onClick={submit}
        disabled={Object.keys(choices).length < output.flagged.length}
        className="rounded bg-foreground px-2 py-1 text-xs text-background disabled:opacity-50"
      >
        Commit measure
      </button>
    </div>
  )
}
