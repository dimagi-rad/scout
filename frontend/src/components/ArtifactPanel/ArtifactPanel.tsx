import { ArtifactViewer } from "@/components/ArtifactViewer"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog"
import { useAppStore } from "@/store/store"

export function ArtifactPanel() {
  const artifactId = useAppStore((s) => s.activeArtifactId)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const closeArtifact = useAppStore((s) => s.uiActions.closeArtifact)
  const isOpen = artifactId !== null

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        if (!open) closeArtifact()
      }}
    >
      <DialogContent
        className="h-[min(86vh,900px)] max-h-[86vh] w-[min(92vw,1200px)] max-w-none gap-0 overflow-hidden p-0"
        showCloseButton={false}
        data-testid="artifact-modal"
      >
        <DialogTitle className="sr-only">Artifact preview</DialogTitle>
        <DialogDescription className="sr-only">
          Interactive artifact preview with data and export controls.
        </DialogDescription>
        {artifactId && activeDomainId && (
          <ArtifactViewer
            artifactId={artifactId}
            workspaceId={activeDomainId}
            onClose={closeArtifact}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}
