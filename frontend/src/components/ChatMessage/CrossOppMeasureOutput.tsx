import { useState } from "react"
import { SqlHighlighter } from "./SqlHighlighter"
import { approveMeasure } from "@/api/crossopp"
import { useAppStore } from "@/store/store"

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
  | { status: "committed"; measure?: string; field?: string; lineage: Lineage[] }
  | {
      status: "needs_approval"
      draft_id: string
      measure: string
      flagged: Flagged[]
      resolved: { opp_id: string; column: string | null; confidence: number }[]
    }
  | { status: "exists"; measure?: string; field?: string; message?: string }
  | {
      status: "needs_approval_redefine"
      draft_id: string
      field: string
      per_opp: { opp_id: string; sql_expression: string | null; status: string }[]
      message?: string
    }
  | {
      status: "proposed"
      committed: string[]
      needs_approval: { measure: string; draft_id: string; flagged: string[] }[]
      message?: string
    }

export function CrossOppMeasureOutput({
  workspaceId,
  output,
}: {
  workspaceId: string
  output: MeasureOutput
}) {
  if (output.status === "proposed") {
    return (
      <div data-testid="crossopp-proposed" className="space-y-1 text-xs">
        {output.committed.length > 0 && (
          <div>
            <span className="font-medium">Committed:</span>{" "}
            {output.committed.join(", ")}
          </div>
        )}
        {output.needs_approval.length > 0 && (
          <div>
            <span className="font-medium">Needs approval:</span>{" "}
            {output.needs_approval.map((n) => n.measure).join(", ")}
          </div>
        )}
        {output.message && <div className="text-muted-foreground">{output.message}</div>}
      </div>
    )
  }
  if (output.status === "exists") {
    const name = output.measure ?? output.field
    return (
      <div className="text-xs text-muted-foreground">
        {output.field ? "Field" : "Measure"} &ldquo;{name}&rdquo; already defined.
      </div>
    )
  }
  if (output.status === "committed") {
    const name = output.measure ?? output.field ?? "measure"
    return (
      <LineageTable
        workspaceId={workspaceId}
        measure={name}
        rows={output.lineage}
        testid={`crossopp-measure-output-${name}`}
      />
    )
  }
  if (output.status === "needs_approval_redefine") {
    return <RedefineApprovalCard workspaceId={workspaceId} output={output} />
  }
  return <ApprovalCard workspaceId={workspaceId} output={output} />
}

function RedefineApprovalCard({
  workspaceId,
  output,
}: {
  workspaceId: string
  output: Extract<MeasureOutput, { status: "needs_approval_redefine" }>
}) {
  const [done, setDone] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const commit = async () => {
    try {
      await approveMeasure(workspaceId, output.draft_id, {})
      setDone(true)
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
        Redefined &ldquo;{output.field}&rdquo;. The cube reloaded — re-open the chart to see the
        corrected curve.
      </div>
    )
  }

  return (
    <div
      data-testid={`crossopp-redefine-${output.draft_id}`}
      className="space-y-2 rounded border border-sky-300 bg-sky-50/50 p-2"
    >
      <div className="text-xs font-medium">
        Redefine &ldquo;{output.field}&rdquo; — confirm the new SQL for each opportunity
      </div>
      {/* Per-opp derived SQL: stacked full-width blocks so the long date-diff expression
          has room to wrap legibly instead of clipping in a narrow table cell. */}
      <div className="space-y-1.5">
        {output.per_opp.map((o) => (
          <div key={o.opp_id} className="overflow-hidden rounded border border-border/50">
            <div className="border-b border-border/50 bg-muted/40 px-2 py-1 font-mono text-[11px] font-medium">
              opp {o.opp_id}
            </div>
            <div className="whitespace-pre-wrap break-words bg-zinc-900 px-2 py-1.5">
              {o.sql_expression ? (
                <SqlHighlighter sql={o.sql_expression} />
              ) : (
                <span className="text-xs text-amber-500">could not resolve for this opp</span>
              )}
            </div>
          </div>
        ))}
      </div>
      {err && <div className="text-red-600">{err}</div>}
      <button
        type="button"
        data-testid={`crossopp-redefine-commit-${output.draft_id}`}
        onClick={commit}
        className="rounded bg-foreground px-2 py-1 text-xs text-background"
      >
        Commit redefinition
      </button>
    </div>
  )
}

function LineageTable({
  workspaceId,
  measure,
  rows,
  testid,
}: {
  workspaceId?: string
  measure: string
  rows: Lineage[]
  testid: string
}) {
  const setPendingChatInput = useAppStore((s) => s.uiActions.setPendingChatInput)
  const editDefinition = () => {
    setPendingChatInput(
      `Redefine the "${measure}" measure as a derived formula — the number of days between ` +
        `two date fields. For an infant age, that is the days between the child's date of birth ` +
        `(child_dob) and the visit date (visit_date), instead of the current single column.`,
    )
  }
  return (
    <div data-testid={testid} className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-medium">{measure} — per-opportunity mapping</div>
        {workspaceId && (
          <button
            type="button"
            data-testid={`crossopp-edit-definition-${measure}`}
            onClick={editDefinition}
            className="rounded border px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-muted/40"
          >
            Edit definition
          </button>
        )}
      </div>
      <div className="overflow-x-auto rounded border border-border/50">
        <table className="w-full text-xs">
          <thead>
            <tr className="bg-muted/40">
              <th className="px-2 py-1 text-left">Opportunity</th>
              <th className="px-2 py-1 text-left">Source field</th>
              <th className="px-2 py-1 text-left">Label</th>
              <th className="px-2 py-1 text-right">Confidence</th>
              <th className="px-2 py-1 text-left">Derivation SQL</th>
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
              onChange={(e) => {
                if (e.target.value === "") return
                setChoices((c) => ({
                  ...c,
                  [f.opp_id]: { action: "pick", column: e.target.value },
                }))
              }}
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
