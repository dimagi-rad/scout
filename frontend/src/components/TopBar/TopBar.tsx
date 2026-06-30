// frontend/src/components/TopBar/TopBar.tsx
import { useState } from "react"
import type { ReactNode } from "react"
import { WorkspaceSwitcher } from "@/components/WorkspaceSwitcher"
import { TopBarSlotContext } from "./TopBarContext"

export function TopBarProvider({ children }: { children: ReactNode }) {
  const [slotEl, setSlotEl] = useState<HTMLElement | null>(null)

  return (
    <TopBarSlotContext.Provider value={slotEl}>
      <div
        className="flex h-11 min-w-0 shrink-0 items-center justify-between gap-2 overflow-hidden border-b px-3 sm:px-4"
        data-testid="top-bar"
      >
        <div className="min-w-0 flex-1" />
        <div className="flex min-w-0 flex-1 items-center justify-end gap-2">
          <div
            ref={setSlotEl}
            className="flex min-w-0 items-center gap-2"
            data-testid="top-bar-slot"
          />
          <WorkspaceSwitcher variant="topbar" />
        </div>
      </div>
      {children}
    </TopBarSlotContext.Provider>
  )
}
