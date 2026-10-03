import { useCallback, useState } from "react"

const STORAGE_KEY = "scout.connectedAccounts.expanded"

function readStored(): Record<string, boolean> {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}")
    return parsed && typeof parsed === "object" ? (parsed as Record<string, boolean>) : {}
  } catch {
    return {}
  }
}

/**
 * Which connection cards this viewer opened or closed, remembered in this browser.
 * Only explicit choices are stored, so a card's default can still depend on its state.
 */
export function useExpandedConnections() {
  const [choices, setChoices] = useState<Record<string, boolean>>(readStored)

  const setExpanded = useCallback((id: string, expanded: boolean) => {
    setChoices((prev) => {
      const next = { ...prev, [id]: expanded }
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
      } catch {
        // Storage can be unavailable (private mode); the choice still holds for this visit.
      }
      return next
    })
  }, [])

  return { choices, setExpanded }
}
