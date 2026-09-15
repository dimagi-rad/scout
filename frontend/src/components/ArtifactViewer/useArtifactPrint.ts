import { useCallback, useEffect, useRef, useState } from "react"

import "./artifactPrint.css"

type PrintCleanup = () => void

// Only one selected artifact may own a document's print layout at a time.
const activePrints = new WeakMap<Document, PrintCleanup>()
const PRINT_ATTRIBUTE = "data-scout-print"

/** Prepare the existing DOM, not a second renderer: printing must not rerun queries. */
function prepareArtifactPrint(target: HTMLElement, onEnd: () => void): PrintCleanup | null {
  const document = target.ownerDocument
  const window = document.defaultView
  if (!window || !target.isConnected || activePrints.has(document)) return null

  const previousAttributes: Array<[HTMLElement, string | null]> = []
  let element: HTMLElement | null = target
  while (element) {
    previousAttributes.push([element, element.getAttribute(PRINT_ATTRIBUTE)])
    element.setAttribute(PRINT_ATTRIBUTE, element === target ? "target" : "ancestor")
    element = element.parentElement
  }

  const media = window.matchMedia?.("print")
  let finished = false
  const cleanup = () => {
    if (finished) return
    finished = true
    window.removeEventListener("afterprint", cleanup)
    window.removeEventListener("pagehide", cleanup)
    media?.removeEventListener("change", handleMediaChange)
    for (const [node, previous] of previousAttributes) {
      if (previous === null) node.removeAttribute(PRINT_ATTRIBUTE)
      else node.setAttribute(PRINT_ATTRIBUTE, previous)
    }
    activePrints.delete(document)
    onEnd()
  }
  function handleMediaChange(event: MediaQueryListEvent) {
    if (!event.matches) cleanup()
  }

  window.addEventListener("afterprint", cleanup)
  window.addEventListener("pagehide", cleanup)
  media?.addEventListener("change", handleMediaChange)
  activePrints.set(document, cleanup)
  return cleanup
}

export function useArtifactPrint(artifactId: string) {
  const targetRef = useRef<HTMLDivElement | null>(null)
  const cleanupRef = useRef<PrintCleanup | null>(null)
  const [printError, setPrintError] = useState<string | null>(null)

  const cancelPrint = useCallback(() => cleanupRef.current?.(), [])
  const printRef = useCallback((node: HTMLDivElement | null) => {
    if (targetRef.current !== node) cancelPrint()
    targetRef.current = node
  }, [cancelPrint])

  useEffect(() => cancelPrint, [artifactId, cancelPrint])

  const printArtifact = useCallback(() => {
    if (cleanupRef.current) return
    setPrintError(null)
    const target = targetRef.current
    if (!target?.isConnected) {
      setPrintError("The artifact is not ready to export. Wait for it to load, then try again.")
      return
    }

    const cleanup = prepareArtifactPrint(target, () => { cleanupRef.current = null })
    if (!cleanup) return
    cleanupRef.current = cleanup
    try {
      target.ownerDocument.defaultView!.print()
      // Do not clean up just because print() returned: some browsers open a
      // nonblocking dialog. afterprint covers both printing and cancellation;
      // leaving print media, changing artifacts, and unmounting also clean up.
    } catch {
      cleanup()
      setPrintError("Could not open the print dialog. Please try again.")
    }
  }, [])

  return { printRef, printArtifact, printError }
}
