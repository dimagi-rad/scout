import { api } from "./client"

export interface Percentiles {
  p50: number | null
  p95: number | null
}

export interface UsageDashboard {
  window: { start: string; end: string; days: number }
  days: string[]
  active_users: { daily: number[]; dau: number; wau: number; mau: number }
  features: Record<"artifact_views" | "recipe_runs" | "workspace_switches" | "logins" | "turns", number[]>
  created: Record<"threads" | "artifacts" | "tenants" | "workspaces", number[]>
  // null for a day with no nightly snapshot yet.
  updated: Record<"threads" | "artifacts" | "tenants" | "workspaces", (number | null)[]>
  turns: {
    total: number
    background: number
    outcomes: { completed: number; stopped: number; failed: number }
    duration_ms: Percentiles
    ttft_ms: Percentiles
    tool_calls_per_turn: number | null
    tokens: { input: number; output: number; cache_read: number }
    daily: { duration_p50_ms: number | null; duration_p95_ms: number | null; ttft_p50_ms: number | null }[]
  }
  tools: {
    name: string
    calls: number
    errors: number
    error_rate: number
    p50_ms: number | null
    p95_ms: number | null
  }[]
  tokens_by_workspace: {
    workspace_id: string
    name: string
    input_tokens: number
    output_tokens: number
    runs: number
  }[]
  loads: {
    total: number
    failed: number
    duration_ms: Percentiles
    phases: { phase: string; p50_ms: number | null; p95_ms: number | null }[]
  }
  materializations: { total: number; states: Record<string, number>; duration_ms: Percentiles }
  recipe_runs: { total: number; failed: number; duration_ms: Percentiles }
  schema_sizes: {
    as_of: string | null
    // Of the latest total, bytes in schemas not serving queries (failed loads, teardowns).
    retained_bytes: number | null
    total_daily: (number | null)[]
    top_tenants: { tenant_id: string; name: string; bytes: number }[]
  }
}

export const usageApi = {
  dashboard: (days: number, signal?: AbortSignal) =>
    api.get<UsageDashboard>(`/api/telemetry/dashboard/?days=${days}`, signal),
}
