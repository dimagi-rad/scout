import { useCallback, useEffect, useRef, useState } from "react"

import { ArtifactViewer } from "@/components/ArtifactViewer"
import { useAppStore } from "@/store/store"

const MIN_WIDTH = 320
const DEFAULT_WIDTH = 600
const MAX_WIDTH_RATIO = 0.75

export function ArtifactPanel() {
  const artifactId = useAppStore((s) => s.activeArtifactId)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const closeArtifact = useAppStore((s) => s.uiActions.closeArtifact)
  const isOpen = artifactId !== null

  const [panelWidth, setPanelWidth] = useState(DEFAULT_WIDTH)
  const [isResizing, setIsResizing] = useState(false)
  const panelRef = useRef<HTMLElement>(null)

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
      {isResizing && (
        <div className="fixed inset-0 z-50 cursor-col-resize" />
      )}
      <aside
        ref={panelRef}
        className={`relative shrink-0 overflow-hidden border-l border-border ${
          isOpen ? "" : "w-0 border-l-0"
        }`}
        style={isOpen ? { width: panelWidth } : { width: 0 }}
      >
        {isOpen && (
          <div
            onMouseDown={handleMouseDown}
            className="absolute bottom-0 left-0 top-0 z-10 w-1.5 cursor-col-resize transition-colors hover:bg-primary/20 active:bg-primary/30"
            data-testid="artifact-panel-resize"
          />
        )}
        {artifactId && activeDomainId && (
          <ArtifactViewer
            artifactId={artifactId}
            workspaceId={activeDomainId}
            onClose={closeArtifact}
          />
        )}
      </aside>
    </>
  )
}
