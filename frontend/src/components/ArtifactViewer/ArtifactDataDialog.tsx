import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { ArtifactDataPanel } from "./ArtifactDataPanel"
import type { QueryDataResponse } from "./types"

interface ArtifactDataDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  artifactTitle?: string
  queryData: QueryDataResponse | null
  isLoading: boolean
  error: string | null
  onRefresh: () => void
}

export function ArtifactDataDialog({
  open,
  onOpenChange,
  artifactTitle,
  queryData,
  isLoading,
  error,
  onRefresh,
}: ArtifactDataDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[min(76vh,44rem)] w-[calc(100vw-2rem)] max-w-5xl flex-col gap-0 overflow-hidden rounded-xl border-border p-0 shadow-2xl sm:w-[calc(100vw-4rem)] sm:rounded-xl">
        <DialogHeader className="border-b border-border px-5 py-4">
          <DialogTitle>Artifact Data</DialogTitle>
          <DialogDescription className="truncate">
            {artifactTitle ?? "Semantic query results and embedded data"}
          </DialogDescription>
        </DialogHeader>
        <ArtifactDataPanel
          queryData={queryData}
          isLoading={isLoading}
          error={error}
          onRefresh={onRefresh}
        />
      </DialogContent>
    </Dialog>
  )
}
