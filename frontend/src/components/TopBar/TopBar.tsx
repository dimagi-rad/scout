// frontend/src/components/TopBar/TopBar.tsx
import { useState } from "react"
import type { ReactNode } from "react"
import { WorkspaceBadge } from "@/components/WorkspaceBadge"
import { TopBarSlotContext } from "./TopBarContext"

export function TopBarProvider({ children }: { children: ReactNode }) {
  const [slotEl, setSlotEl] = useState<HTMLElement | null>(null)

  return (
    <TopBarSlotContext.Provider value={slotEl}>
      <div
        className="flex h-11 shrink-0 items-center justify-between border-b px-4"
        data-testid="top-bar"
      >
        <div className="flex items-center gap-2" />
        <div className="flex items-center gap-2">
          <div
            ref={setSlotEl}
            className="flex items-center gap-2"
            data-testid="top-bar-slot"
          />
          <WorkspaceBadge />
        </div>
      </div>
      {children}
    </TopBarSlotContext.Provider>
  )
}
