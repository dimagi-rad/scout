import { useContext } from "react"
import type { ReactNode } from "react"
import { createPortal } from "react-dom"
import { TopBarSlotContext } from "./TopBarContext"

/**
 * Renders children into the global TopBar's trailing slot.
 * No-op until the TopBar has mounted (slot element exists).
 */
export function TopBarSlot({ children }: { children: ReactNode }) {
  const slotEl = useContext(TopBarSlotContext)
  if (!slotEl) return null
  return createPortal(children, slotEl)
}
