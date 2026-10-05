import { useCallback, useEffect, useState } from "react"

import { api, asRecord } from "@/api/client"
import { Button } from "@/components/ui/button"
import { CONNECTIONS_PATH } from "@/lib/routes"
import {
  oauthConnectUrl,
  postOAuthStart,
  startOAuthOnClick,
  type OAuthProvider,
} from "@/lib/oauth"

interface OcsTeam {
  slug: string
  name: string
}

type StopReason = "mismatch" | "cancelled" | "failed" | "incomplete" | "user" | "idle"

interface OcsTeamFlow {
  mode: "all" | "one"
  connected: OcsTeam[]
  remaining: OcsTeam[]
  /** A hop the server hasn't seen come back yet. */
  pending: OcsTeam | null
  finished: boolean
  stopped: { reason: StopReason; team: OcsTeam; got: OcsTeam | null } | null
}

/** GET /api/auth/ocs/teams/ */
export interface OcsTeamsState {
  /** False until an OCS connect has returned the `teams` claim. */
  known: boolean
  teams: (OcsTeam & { connected: boolean })[]
  flow: OcsTeamFlow | null
  /** The team the "connect all" chain goes to next, if it should continue. */
  next: string | null
}

/** Long enough to read the progress line and press Stop before the next hop. */
export const CHAIN_CONTINUE_DELAY_MS = 1500
/** How often to re-check a hop the server hasn't settled yet. */
export const PENDING_RECHECK_MS = 5000

const isTeam = (value: unknown): value is OcsTeam =>
  typeof asRecord(value)?.slug === "string"

function parseFlow(value: unknown): OcsTeamFlow | null {
  const flow = asRecord(value)
  if (!flow || !Array.isArray(flow.connected) || !Array.isArray(flow.remaining)) return null
  const stopped = asRecord(flow.stopped)
  return {
    mode: flow.mode === "all" ? "all" : "one",
    connected: flow.connected.filter(isTeam),
    remaining: flow.remaining.filter(isTeam),
    pending: isTeam(flow.pending) ? flow.pending : null,
    finished: flow.finished === true,
    stopped:
      stopped && isTeam(stopped.team)
        ? {
            reason: stopped.reason as StopReason,
            team: stopped.team,
            got: isTeam(stopped.got) ? stopped.got : null,
          }
        : null,
  }
}

function parseState(value: unknown): OcsTeamsState | null {
  const record = asRecord(value)
  if (!record || !Array.isArray(record.teams)) return null
  return {
    known: record.known === true,
    teams: record.teams
      .filter(isTeam)
      .map((t) => ({ ...t, connected: asRecord(t)?.connected === true })),
    flow: parseFlow(record.flow),
    next: typeof record.next === "string" ? record.next : null,
  }
}

function stopMessage(stopped: NonNullable<OcsTeamFlow["stopped"]>): string {
  const team = stopped.team.name
  switch (stopped.reason) {
    case "mismatch":
      return stopped.got
        ? `Open Chat Studio returned team "${stopped.got.name}" instead of "${team}", so Scout didn't connect it. You may no longer be a member of "${team}".`
        : `Open Chat Studio didn't confirm the team "${team}", so Scout didn't connect it.`
    case "cancelled":
      return `Connecting "${team}" was cancelled on Open Chat Studio.`
    case "failed":
      return `Connecting "${team}" failed.`
    case "incomplete":
      return `Connecting "${team}" didn't finish.`
    case "user":
      return `Stopped before "${team}".`
    case "idle":
      return `Paused before "${team}".`
    default:
      return `Connecting "${team}" stopped.`
  }
}

export function OcsTeamsPanel({ provider }: { provider: OAuthProvider }) {
  const [state, setState] = useState<OcsTeamsState | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setState(parseState(await api.get("/api/auth/ocs/teams/")))
      setError(null)
    } catch {
      // Keep what was shown: hiding the panel would also hide a running chain's Stop.
      setError("Couldn't load your Open Chat Studio teams.")
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const startTeam = useCallback(
    (slug: string) => postOAuthStart(oauthConnectUrl(provider, CONNECTIONS_PATH, slug)),
    [provider],
  )

  const next = state?.next ?? null
  useEffect(() => {
    if (!next) return
    const timer = window.setTimeout(() => {
      startTeam(next).catch(async () => {
        // Stopped server-side too, so a reload can't retry it in a loop.
        setState((prev) => prev && { ...prev, next: null })
        try {
          const stopped = parseState(await api.post("/api/auth/ocs/teams/stop/"))
          if (stopped) setState(stopped)
        } catch {
          // The server's idle timeout stops it anyway.
        }
        setError("Couldn't start the next team.")
      })
    }, CHAIN_CONTINUE_DELAY_MS)
    return () => window.clearTimeout(timer)
  }, [next, startTeam])

  const pendingSlug = state?.flow?.pending?.slug
  useEffect(() => {
    if (!pendingSlug) return
    const timer = window.setTimeout(() => void load(), PENDING_RECHECK_MS)
    return () => window.clearTimeout(timer)
  }, [pendingSlug, state, load])

  async function post(path: string): Promise<OcsTeamsState | null> {
    setBusy(true)
    setError(null)
    try {
      return parseState(await api.post(path))
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed.")
      return null
    } finally {
      setBusy(false)
    }
  }

  async function connectAll() {
    // Each hop, the first included, starts from the `next` effect above.
    const started = await post("/api/auth/ocs/teams/connect-all/")
    if (started) setState(started)
  }

  async function stopChain() {
    // Cancel the hop timer now; it could fire while the POST is in flight.
    setState((prev) => prev && { ...prev, next: null })
    const stopped = await post("/api/auth/ocs/teams/stop/")
    if (stopped) setState(stopped)
  }

  async function dismiss() {
    setBusy(true)
    try {
      await api.post("/api/auth/ocs/teams/dismiss/")
    } catch {
      // The banner is only a message; reloading shows whatever the server kept.
    } finally {
      setBusy(false)
    }
    await load()
  }

  if (!state) {
    return error ? (
      <p className="text-sm text-destructive" data-testid="ocs-teams-error">
        {error}
      </p>
    ) : null
  }

  if (!state.known) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="ocs-teams-hint">
        Reconnect one Open Chat Studio team to list all your teams here, so you can connect the
        rest in one go.
      </p>
    )
  }

  const unconnected = state.teams.filter((t) => !t.connected)
  const flow = state.flow
  const chainRunning = flow?.mode === "all" && !flow.stopped && !flow.finished
  const nextName = flow?.remaining.find((t) => t.slug === next)?.name ?? next

  return (
    <div className="space-y-3 border-t pt-3" data-testid="ocs-teams-panel">
      {flow && (
        <div
          className="flex flex-wrap items-center justify-between gap-3 rounded-md border bg-muted/40 p-3"
          data-testid="ocs-teams-flow"
        >
          <div className="min-w-0 flex-1 space-y-1 text-sm">
            {flow.connected.length > 0 && (
              <p data-testid="ocs-teams-flow-connected">
                Connected {flow.connected.map((t) => t.name).join(", ")}.
              </p>
            )}
            {flow.stopped ? (
              <p className="text-amber-600" data-testid="ocs-teams-flow-stopped">
                {stopMessage(flow.stopped)}
              </p>
            ) : flow.finished ? (
              <p data-testid="ocs-teams-flow-finished">All your teams are connected.</p>
            ) : (
              <>
                {flow.pending && (
                  <p data-testid="ocs-teams-flow-pending">
                    Waiting for &quot;{flow.pending.name}&quot; to finish on Open Chat Studio…
                  </p>
                )}
                {chainRunning && next && (
                  <p data-testid="ocs-teams-flow-next">
                    Connecting {nextName}
                    {flow.remaining.length > 1 ? ` (${flow.remaining.length} left)` : ""}…
                  </p>
                )}
              </>
            )}
          </div>
          <div className="flex shrink-0 gap-2">
            {chainRunning ? (
              <Button
                variant="outline"
                size="sm"
                onClick={() => void stopChain()}
                disabled={busy}
                data-testid="ocs-teams-stop"
              >
                Stop
              </Button>
            ) : (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void dismiss()}
                disabled={busy}
                data-testid="ocs-teams-dismiss"
              >
                Dismiss
              </Button>
            )}
          </div>
        </div>
      )}

      {error && (
        <p className="text-sm text-destructive" data-testid="ocs-teams-error">
          {error}
        </p>
      )}

      {unconnected.length > 0 && !chainRunning && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm font-medium">Your other Open Chat Studio teams</p>
            {unconnected.length > 1 && (
              <Button
                size="sm"
                onClick={() => void connectAll()}
                disabled={busy}
                data-testid="ocs-teams-connect-all"
              >
                {flow?.mode === "all" && flow.stopped ? "Resume" : "Connect all remaining teams"}{" "}
                ({unconnected.length})
              </Button>
            )}
          </div>
          <ul className="divide-y rounded-md border">
            {unconnected.map((team) => (
              <li
                key={team.slug}
                className="flex items-center justify-between gap-3 px-3 py-2"
                data-testid={`ocs-team-${team.slug}`}
              >
                <span className="min-w-0 truncate text-sm">{team.name}</span>
                <Button
                  variant="outline"
                  size="sm"
                  asChild
                  data-testid={`ocs-team-connect-${team.slug}`}
                >
                  <a
                    href={oauthConnectUrl(provider, CONNECTIONS_PATH, team.slug)}
                    onClick={startOAuthOnClick}
                  >
                    Connect
                  </a>
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
