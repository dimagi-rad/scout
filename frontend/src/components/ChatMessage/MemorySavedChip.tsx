import { Link, useLocation } from "react-router-dom"
import { Brain } from "lucide-react"
import type { MemoryLayer, SavedMemory } from "./savedMemory"

const LAYER_LABELS: Record<MemoryLayer, string> = {
  personal: "Personal",
  workspace: "Workspace",
}

export function MemorySavedChip({ layer, memory }: SavedMemory) {
  const location = useLocation()
  const prefix = location.pathname.startsWith("/embed") ? "/embed" : ""
  return (
    <div
      className="my-1 inline-flex max-w-full items-center gap-2 rounded-full border border-violet-200 bg-violet-50 px-3 py-1 text-xs text-violet-900 dark:border-violet-800 dark:bg-violet-950/40 dark:text-violet-200"
      data-testid="memory-saved-chip"
      data-layer={layer}
    >
      <Brain className="h-3.5 w-3.5 shrink-0" />
      <span className="shrink-0 font-medium">
        Saved to memory · <span data-testid="memory-saved-chip-layer">{LAYER_LABELS[layer]}</span>
      </span>
      <span className="min-w-0 truncate text-violet-800/80 dark:text-violet-200/80" title={memory}>
        {memory}
      </span>
      <Link
        to={`${prefix}/memory`}
        className="shrink-0 underline underline-offset-2"
        data-testid="memory-saved-chip-link"
      >
        Manage
      </Link>
    </div>
  )
}
