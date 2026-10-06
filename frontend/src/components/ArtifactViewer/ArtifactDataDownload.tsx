import { useState } from "react"
import { Download, Loader2 } from "lucide-react"

import { api } from "@/api/client"
import type { ArtifactQueryContext } from "@/components/ArtifactGraph/types"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { useWorkspaceRole } from "@/hooks/useWorkspaceRole"

export interface ArtifactDataDownloadTarget {
  artifactId: string
  workspaceId: string
  /** The date controls the viewer has chosen, so the file matches the screen. */
  runtime?: ArtifactQueryContext
}

interface ExportDataset {
  name: string
  source: "query" | "static"
}

interface ExportListing {
  datasets: ExportDataset[]
  row_limit: number
}

function filenameFrom(headers: Headers, fallback: string): string {
  const match = headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)
  return match?.[1] ?? fallback
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

export function ArtifactDataDownload({ artifactId, workspaceId, runtime }: ArtifactDataDownloadTarget) {
  const { canWrite } = useWorkspaceRole(workspaceId)
  const [listing, setListing] = useState<ExportListing | null>(null)
  const [loadingList, setLoadingList] = useState(false)
  const [downloading, setDownloading] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ tone: "info" | "error"; text: string } | null>(null)

  // The server enforces READ_WRITE; read-only members never see the control.
  if (!canWrite) return null

  const base = `/api/workspaces/${workspaceId}/artifacts/${artifactId}/data-export/`

  async function loadDatasets() {
    setLoadingList(true)
    setNotice(null)
    try {
      setListing(runtime ? await api.post<ExportListing>(base, runtime) : await api.get<ExportListing>(base))
    } catch (e) {
      setNotice({ tone: "error", text: e instanceof Error ? e.message : "Could not list the artifact's data." })
    } finally {
      setLoadingList(false)
    }
  }

  async function download(dataset: ExportDataset) {
    setDownloading(dataset.name)
    setNotice(null)
    try {
      const url = `${base}csv/?${dataset.source}=${encodeURIComponent(dataset.name)}`
      const { blob, headers } = await api.download(url, runtime)
      saveBlob(blob, filenameFrom(headers, `${dataset.name}.csv`))
      if (headers.get("X-Scout-Export-Truncated") === "true") {
        const limit = Number(headers.get("X-Scout-Export-Row-Limit") ?? listing?.row_limit ?? 0)
        setNotice({
          tone: "info",
          text: `Only the first ${limit.toLocaleString()} rows of "${dataset.name}" were downloaded. Add filters to narrow it.`,
        })
      }
    } catch (e) {
      setNotice({ tone: "error", text: e instanceof Error ? e.message : "Download failed." })
    } finally {
      setDownloading(null)
    }
  }

  return (
    <div className="flex items-center gap-2">
      {notice && (
        <span
          role={notice.tone === "error" ? "alert" : "status"}
          className={notice.tone === "error"
            ? "max-w-xs text-xs text-destructive"
            : "max-w-xs text-xs text-muted-foreground"}
          data-testid="artifact-download-notice"
        >
          {notice.text}
        </span>
      )}
      <DropdownMenu onOpenChange={(open) => { if (open) void loadDatasets() }}>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={downloading !== null}
            title="Download the data behind this artifact as CSV"
            data-testid="artifact-download-data"
          >
            {downloading !== null ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
            Download data
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" data-testid="artifact-download-menu">
          <DropdownMenuLabel>CSV, one file per dataset</DropdownMenuLabel>
          {loadingList && <DropdownMenuItem disabled>Loading…</DropdownMenuItem>}
          {!loadingList && listing?.datasets.length === 0 && (
            <DropdownMenuItem disabled>No downloadable data</DropdownMenuItem>
          )}
          {!loadingList && listing?.datasets.map((dataset) => (
            <DropdownMenuItem
              key={`${dataset.source}:${dataset.name}`}
              onSelect={() => void download(dataset)}
              data-testid={`artifact-download-dataset-${dataset.name}`}
            >
              {dataset.name}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}
