import { useState, useEffect, useCallback, useRef } from "react"
import { useAppStore } from "@/store/store"
import { X, Eye, Database, RefreshCw, Loader2, FileDown } from "lucide-react"
import { api } from "@/api/client"
import { withBasePath } from "@/config"

interface QueryResult {
  name: string
  sql?: string
  semantic_query?: Record<string, unknown>
  columns?: string[]
  rows?: unknown[][]
  row_count?: number
  truncated?: boolean
  error?: string
}

interface QueryDataResponse {
  queries: QueryResult[]
  static_data: Record<string, unknown>
}

type Tab = "view" | "data"

const MIN_WIDTH = 320
const DEFAULT_WIDTH = 600
const MAX_WIDTH_RATIO = 0.75

export function ArtifactPanel() {
  const artifactId = useAppStore((s) => s.activeArtifactId)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const closeArtifact = useAppStore((s) => s.uiActions.closeArtifact)
  const isOpen = artifactId !== null

  const [activeTab, setActiveTab] = useState<Tab>("view")
  const [queryData, setQueryData] = useState<QueryDataResponse | null>(null)
  const [dataLoading, setDataLoading] = useState(false)
  const [dataError, setDataError] = useState<string | null>(null)
  const [panelWidth, setPanelWidth] = useState(DEFAULT_WIDTH)
  const [isResizing, setIsResizing] = useState(false)
  const panelRef = useRef<HTMLElement>(null)
  const iframeRef = useRef<HTMLIFrameElement>(null)

  const handleExportPdf = useCallback(() => {
    // Trigger print inside the sandboxed iframe so the print job is scoped to
    // the artifact content, not the surrounding app. The sandbox HTML listens
    // for this message and calls window.print() (browser "Save as PDF").
    //
    // targetOrigin is "*" because the iframe is sandboxed WITHOUT
    // allow-same-origin and therefore has an opaque ("null") security origin.
    // The browser drops a postMessage whose targetOrigin is a concrete origin
    // string (e.g. window.location.origin) when the target frame's security
    // origin is opaque, which would silently break Export PDF. "*" is safe
    // here: the message is a trivial non-secret "scout-print" signal sent only
    // to our own artifact iframe via iframeRef, so there is nothing to leak.
    iframeRef.current?.contentWindow?.postMessage({ type: "scout-print" }, "*")
  }, [])

  const fetchQueryData = useCallback(async (id: string) => {
    if (!activeDomainId) return
    setDataLoading(true)
    setDataError(null)
    try {
      const data = await api.get<QueryDataResponse>(`/api/workspaces/${activeDomainId}/artifacts/${id}/query-data/`)
      setQueryData(data)
    } catch (e) {
      setDataError(e instanceof Error ? e.message : "Failed to load query data")
    } finally {
      setDataLoading(false)
    }
  }, [activeDomainId])

  useEffect(() => {
    if (artifactId && activeTab === "data") {
      fetchQueryData(artifactId)
    }
  }, [artifactId, activeTab, fetchQueryData])

  useEffect(() => {
    function handleMessage(event: MessageEvent) {
      // Only trust messages that came from our own artifact iframe. The iframe
      // runs agent-generated (prompt-injectable) code, and it is sandboxed
      // WITHOUT allow-same-origin, so it is a unique opaque origin whose
      // event.origin is the literal string "null" — an origin allowlist would
      // either reject legitimate messages or have to trust "null" (which any
      // sandboxed frame on the page could also claim). The robust check is on
      // the message source: reject anything not posted by this iframe's window
      // (e.g. other frames, popups, or window.opener attempting to forge
      // artifact-query-data).
      if (event.source !== iframeRef.current?.contentWindow) return
      if (event.data?.type === "artifact-query-data" && event.data.artifactId === artifactId) {
        setQueryData(event.data.queryData)
      }
    }
    window.addEventListener("message", handleMessage)
    return () => window.removeEventListener("message", handleMessage)
  }, [artifactId])

  useEffect(() => {
    setActiveTab("view")
    setQueryData(null)
    setDataError(null)
  }, [artifactId])

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    setIsResizing(true)
  }, [])

  useEffect(() => {
    if (!isResizing) return

    const handleMouseMove = (e: MouseEvent) => {
      const maxWidth = window.innerWidth * MAX_WIDTH_RATIO
      const newWidth = Math.max(MIN_WIDTH, Math.min(maxWidth, window.innerWidth - e.clientX))
      setPanelWidth(newWidth)
    }

    const handleMouseUp = () => {
      setIsResizing(false)
    }

    document.addEventListener("mousemove", handleMouseMove)
    document.addEventListener("mouseup", handleMouseUp)
    return () => {
      document.removeEventListener("mousemove", handleMouseMove)
      document.removeEventListener("mouseup", handleMouseUp)
    }
  }, [isResizing])

  return (
    <>
      {/* Full-screen overlay during resize to capture mouse events over iframes */}
      {isResizing && (
        <div className="fixed inset-0 z-50 cursor-col-resize" />
      )}
      <aside
        ref={panelRef}
        className={`relative overflow-hidden border-l border-border shrink-0 ${
          isOpen ? "" : "w-0 border-l-0"
        }`}
        style={isOpen ? { width: panelWidth } : { width: 0 }}
      >
        {/* Resize handle */}
        {isOpen && (
          <div
            onMouseDown={handleMouseDown}
            className="absolute left-0 top-0 bottom-0 w-1.5 cursor-col-resize z-10 hover:bg-primary/20 active:bg-primary/30 transition-colors"
            data-testid="artifact-panel-resize"
          />
        )}
      {artifactId && (
        <div className="flex h-full flex-col">
          {/* Header with tabs */}
          <div className="flex h-14 items-center justify-between border-b border-border px-4">
            <div className="flex items-center gap-1">
              <button
                onClick={() => setActiveTab("view")}
                className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                  activeTab === "view"
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground"
                }`}
                data-testid="artifact-tab-view"
              >
                <Eye className="h-3.5 w-3.5" />
                View
              </button>
              <button
                onClick={() => setActiveTab("data")}
                className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                  activeTab === "data"
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground"
                }`}
                data-testid="artifact-tab-data"
              >
                <Database className="h-3.5 w-3.5" />
                Data
              </button>
            </div>
            <div className="flex items-center gap-1">
              <button
                onClick={handleExportPdf}
                disabled={activeTab !== "view"}
                className="flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40 disabled:hover:bg-transparent transition-colors"
                title={activeTab === "view" ? "Export to PDF" : "Switch to the View tab to export"}
                data-testid="artifact-export-pdf"
              >
                <FileDown className="h-3.5 w-3.5" />
                Export PDF
              </button>
              <button
                onClick={closeArtifact}
                className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                title="Close"
                data-testid="artifact-close"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>

          {/* View tab: iframe */}
          {activeTab === "view" && (
            <iframe
              ref={iframeRef}
              key={artifactId}
              src={activeDomainId ? withBasePath(`/api/workspaces/${activeDomainId}/artifacts/${artifactId}/sandbox/`) : ""}
              className="flex-1 w-full"
              // SECURITY: deliberately NO allow-same-origin. The sandbox doc is
              // served same-origin and session-authenticated, and it executes
              // agent-generated (prompt-injectable) code. With allow-same-origin
              // that code could read the viewer's cookies/CSRF token, issue
              // credentialed /api/ requests, and reach window.parent — a full
              // session takeover. Omitting it gives the frame a unique opaque
              // origin: scripts still run and render UI, but the frame cannot
              // touch the parent origin or send credentialed same-origin
              // requests. Do NOT re-add allow-same-origin.
              sandbox="allow-scripts allow-modals"
              title="Artifact"
            />
          )}

          {/* Data tab: SQL queries and results */}
          {activeTab === "data" && (
            <div className="flex-1 overflow-y-auto">
              <div className="p-4 space-y-4">
                {/* Refresh button */}
                <div className="flex items-center justify-between">
                  <span className="text-sm text-muted-foreground">
                    {queryData?.queries?.length
                      ? `${queryData.queries.length} ${queryData.queries.length === 1 ? "query" : "queries"}`
                      : "No queries stored"}
                  </span>
                  <button
                    onClick={() => fetchQueryData(artifactId)}
                    disabled={dataLoading}
                    className="flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-50 transition-colors"
                    data-testid="artifact-data-refresh"
                  >
                    {dataLoading ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <RefreshCw className="h-3.5 w-3.5" />
                    )}
                    Refresh
                  </button>
                </div>

                {dataError && (
                  <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
                    {dataError}
                  </div>
                )}

                {dataLoading && !queryData && (
                  <div className="flex items-center justify-center py-12 text-muted-foreground">
                    <Loader2 className="h-5 w-5 animate-spin mr-2" />
                    <span className="text-sm">Executing queries...</span>
                  </div>
                )}

                {queryData?.queries?.length === 0 && !dataLoading && (
                  <div className="text-center py-12 text-muted-foreground text-sm">
                    This artifact has no stored queries. Data was embedded statically.
                  </div>
                )}

                {queryData?.queries?.map((q, i) => (
                  <QueryResultCard key={i} query={q} />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </aside>
    </>
  )
}

function QueryResultCard({ query }: { query: QueryResult }) {
  const [expanded, setExpanded] = useState(true)

  return (
    <div className="rounded-lg border border-border overflow-hidden">
      {/* Query header */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center justify-between px-4 py-2.5 bg-muted/50 hover:bg-muted transition-colors text-left"
      >
        <span className="text-sm font-medium">{query.name}</span>
        <span className="text-xs text-muted-foreground">
          {query.error
            ? "Error"
            : `${query.row_count ?? 0} row${query.row_count === 1 ? "" : "s"}${query.truncated ? " (truncated)" : ""}`}
        </span>
      </button>

      {expanded && (
        <div className="divide-y divide-border">
          {/* Query spec */}
          <div className="p-3 bg-muted/20">
            <pre className="text-xs font-mono text-muted-foreground whitespace-pre-wrap overflow-x-auto">
              {query.sql ?? JSON.stringify(query.semantic_query ?? {}, null, 2)}
            </pre>
          </div>

          {/* Error */}
          {query.error && (
            <div className="p-3 text-sm text-destructive bg-destructive/5">
              {query.error}
            </div>
          )}

          {/* Results table */}
          {!query.error && query.columns && query.columns.length > 0 && (
            <div className="overflow-x-auto max-h-80">
              <table className="w-full text-xs">
                <thead className="bg-muted/30 sticky top-0">
                  <tr>
                    {query.columns.map((col) => (
                      <th
                        key={col}
                        className="px-3 py-2 text-left font-medium text-muted-foreground whitespace-nowrap"
                      >
                        {col}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {(query.rows ?? []).map((row, ri) => (
                    <tr key={ri} className="hover:bg-muted/20">
                      {(row as unknown[]).map((cell, ci) => (
                        <td key={ci} className="px-3 py-1.5 whitespace-nowrap">
                          {cell === null ? (
                            <span className="text-muted-foreground italic">null</span>
                          ) : (
                            String(cell)
                          )}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
