import type { ReactNode } from "react"
import {
  ChevronRight,
  FileBarChart,
  FileText,
  LayoutDashboard,
  Loader2,
  PanelsTopLeft,
  RefreshCw,
  X,
  type LucideIcon,
} from "lucide-react"

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
              <LayoutDashboard className="h-4 w-4 text-muted-foreground" />
            ) : (
              <PanelsTopLeft className="h-4 w-4 text-muted-foreground" />
            )}
            <h2 className="truncate text-sm font-semibold">
              {mode === "files" ? "Artifacts" : "Canvas"}
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
                aria-label="Refresh artifacts"
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
        title="Loading artifacts"
      />
    )
  }

  if (status === "error") {
    return (
      <PanelState
        icon={<LayoutDashboard className="h-5 w-5" />}
        title="Artifacts unavailable"
        body={error ?? "Could not load this thread's artifacts."}
        action={<Button size="sm" onClick={onRefresh}>Try Again</Button>}
      />
    )
  }

  if (artifacts.length === 0) {
    return (
      <PanelState
        icon={<LayoutDashboard className="h-5 w-5" />}
        title="No artifacts"
        body="Artifacts created or referenced in this thread will appear here."
      />
    )
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="space-y-1 p-2">
        {artifacts.map((artifact) => (
          <ArtifactListItem
            key={artifact.id}
            artifact={artifact}
            onOpen={() => onOpenArtifact(artifact.id)}
          />
        ))}
      </div>
    </div>
  )
}

function ArtifactListItem({
  artifact,
  onOpen,
}: {
  artifact: ThreadArtifactSummary
  onOpen: () => void
}) {
  const Icon = artifactIcon(artifact.artifact_type)

  return (
    <button
      type="button"
      onClick={onOpen}
      className="group flex w-full items-start gap-3 rounded-md px-2.5 py-2.5 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/35"
    >
      <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md border bg-muted/40 text-muted-foreground transition-colors group-hover:border-primary/30 group-hover:text-primary">
        <Icon className="h-4 w-4" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium text-foreground">
          {artifact.title || "Untitled"}
        </span>
        {artifact.description && (
          <span className="mt-0.5 block line-clamp-2 text-xs leading-5 text-muted-foreground">
            {artifact.description}
          </span>
        )}
      </span>
      <ChevronRight className="mt-2 h-4 w-4 shrink-0 text-muted-foreground/60 opacity-0 transition-opacity group-hover:opacity-100" />
    </button>
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

function artifactIcon(type: string): LucideIcon {
  if (type === "story" || type === "plotly") return FileBarChart
  if (type === "react" || type === "html" || type === "svg") return PanelsTopLeft
  return FileText
}
