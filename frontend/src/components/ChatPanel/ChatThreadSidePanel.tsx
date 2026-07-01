import type { ReactNode } from "react"
import { FileText, Loader2, PanelsTopLeft, RefreshCw, X } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import type { ThreadPanelMode } from "./ChatThreadHeader"

export interface ThreadArtifactSummary {
  id: string
  title: string
  description: string
  artifact_type: string
  version: number
  source: "created" | "updated" | "mentioned" | "attached" | string
  created_at: string
  updated_at: string
  linked_at: string
  last_seen_at: string
}

type LoadStatus = "idle" | "loading" | "loaded" | "error"

interface ChatThreadSidePanelProps {
  open: boolean
  mode: ThreadPanelMode
  artifacts: ThreadArtifactSummary[]
  filesStatus: LoadStatus
  filesError: string | null
  canvas: ReactNode
  onClose: () => void
  onOpenArtifact: (artifactId: string) => void
  onRefreshFiles: () => void
}

export function ChatThreadSidePanel({
  open,
  mode,
  artifacts,
  filesStatus,
  filesError,
  canvas,
  onClose,
  onOpenArtifact,
  onRefreshFiles,
}: ChatThreadSidePanelProps) {
  return (
    <aside
      className={cn(
        "h-full shrink-0 overflow-hidden border-l bg-background transition-[width,opacity] duration-200 ease-out",
        open ? "w-[25rem] opacity-100" : "w-0 opacity-0",
      )}
      aria-hidden={!open}
    >
      <div className="flex h-full w-[25rem] flex-col">
        <div className="flex h-12 shrink-0 items-center justify-between gap-3 border-b px-3">
          <div className="flex min-w-0 items-center gap-2">
            {mode === "files" ? (
              <FileText className="h-4 w-4 text-muted-foreground" />
            ) : (
              <PanelsTopLeft className="h-4 w-4 text-muted-foreground" />
            )}
            <h2 className="truncate text-sm font-semibold">
              {mode === "files" ? "Files" : "Canvas"}
            </h2>
            {mode === "files" && artifacts.length > 0 && (
              <Badge variant="secondary">{artifacts.length}</Badge>
            )}
          </div>
          <div className="flex items-center gap-1">
            {mode === "files" && (
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                onClick={onRefreshFiles}
                disabled={filesStatus === "loading"}
                aria-label="Refresh files"
              >
                <RefreshCw className={cn("h-3.5 w-3.5", filesStatus === "loading" && "animate-spin")} />
              </Button>
            )}
            <Button type="button" variant="ghost" size="icon-xs" onClick={onClose} aria-label="Close panel">
              <X className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>

        {mode === "files" ? (
          <ThreadFilesList
            artifacts={artifacts}
            status={filesStatus}
            error={filesError}
            onOpenArtifact={onOpenArtifact}
            onRefresh={onRefreshFiles}
          />
        ) : (
          <div className="min-h-0 flex-1">{canvas}</div>
        )}
      </div>
    </aside>
  )
}

function ThreadFilesList({
  artifacts,
  status,
  error,
  onOpenArtifact,
  onRefresh,
}: {
  artifacts: ThreadArtifactSummary[]
  status: LoadStatus
  error: string | null
  onOpenArtifact: (artifactId: string) => void
  onRefresh: () => void
}) {
  if (status === "loading" && artifacts.length === 0) {
    return (
      <PanelState
        icon={<Loader2 className="h-5 w-5 animate-spin" />}
        title="Loading files"
      />
    )
  }

  if (status === "error") {
    return (
      <PanelState
        icon={<FileText className="h-5 w-5" />}
        title="Files unavailable"
        body={error ?? "Could not load this thread's files."}
        action={<Button size="sm" onClick={onRefresh}>Try Again</Button>}
      />
    )
  }

  if (artifacts.length === 0) {
    return (
      <PanelState
        icon={<FileText className="h-5 w-5" />}
        title="No files"
        body="Artifacts created or referenced in this thread will appear here."
      />
    )
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="divide-y">
        {artifacts.map((artifact) => (
          <button
            key={artifact.id}
            type="button"
            onClick={() => onOpenArtifact(artifact.id)}
            className="block w-full px-3 py-3 text-left hover:bg-accent"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="truncate text-sm font-medium">{artifact.title || "Untitled"}</div>
                <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                  <span>{artifact.artifact_type}</span>
                  <span>v{artifact.version}</span>
                  <span>{sourceLabel(artifact.source)}</span>
                </div>
              </div>
              <FileText className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
            </div>
            {artifact.description && (
              <p className="mt-2 line-clamp-2 text-xs text-muted-foreground">
                {artifact.description}
              </p>
            )}
          </button>
        ))}
      </div>
    </div>
  )
}

function PanelState({
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
    <div className="flex min-h-0 flex-1 items-center justify-center p-6 text-center text-muted-foreground">
      <div>
        <div className="mb-3 flex justify-center">{icon}</div>
        <h3 className="text-sm font-medium text-foreground">{title}</h3>
        {body && <p className="mt-1 max-w-xs text-sm">{body}</p>}
        {action && <div className="mt-3">{action}</div>}
      </div>
    </div>
  )
}

function sourceLabel(source: ThreadArtifactSummary["source"]): string {
  if (source === "created") return "Created"
  if (source === "updated") return "Updated"
  if (source === "attached") return "Attached"
  return "Mentioned"
}
