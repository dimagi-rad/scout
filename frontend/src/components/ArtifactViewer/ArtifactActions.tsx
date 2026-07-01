import { Database, FileDown, X } from "lucide-react"

import { Button } from "@/components/ui/button"

interface ArtifactActionsProps {
  onViewData: () => void
  onExportPdf: () => void
  onClose?: () => void
  exportDisabled?: boolean
}

export function ArtifactActions({
  onViewData,
  onExportPdf,
  onClose,
  exportDisabled,
}: ArtifactActionsProps) {
  return (
    <div className="flex items-center gap-2">
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={onViewData}
        data-testid="artifact-view-data"
      >
        <Database className="h-4 w-4" />
        View Data
      </Button>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={onExportPdf}
        disabled={exportDisabled}
        title={exportDisabled ? "Return to the artifact view to export" : "Export to PDF"}
        data-testid="artifact-export-pdf"
      >
        <FileDown className="h-4 w-4" />
        Export PDF
      </Button>
      {onClose && (
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          onClick={onClose}
          title="Close"
          data-testid="artifact-close"
        >
          <X className="h-4 w-4" />
        </Button>
      )}
    </div>
  )
}
