import { useState } from "react"
import { Link, useLocation } from "react-router-dom"
import { Brain } from "lucide-react"
import { personalMemoryApi } from "@/api/memory"
import { ApiError } from "@/api/client"
import type { MemoryLayer, SavedMemory } from "./savedMemory"

const LAYER_LABELS: Record<MemoryLayer, string> = {
  personal: "Personal",
  workspace: "Workspace",
}

type UndoState = "idle" | "pending" | "undone" | "error"

async function forget(layer: MemoryLayer, memoryId: string) {
  if (layer === "personal") await personalMemoryApi.remove(memoryId)
}

export function MemorySavedChip({ layer, memory, memoryId, created }: SavedMemory) {
  const location = useLocation()
  const prefix = location.pathname.startsWith("/embed") ? "/embed" : ""
  const [undo, setUndo] = useState<UndoState>("idle")
  const canUndo = created && !!memoryId && layer === "personal"

  const handleUndo = async () => {
    if (!memoryId || undo === "pending") return
    setUndo("pending")
    try {
      await forget(layer, memoryId)
      setUndo("undone")
    } catch (error) {
      // Already gone (deleted on the Memory page) is what Undo wanted anyway.
      setUndo(error instanceof ApiError && error.status === 404 ? "undone" : "error")
    }
  }

  return (
    <div
      className="my-1 inline-flex max-w-full items-center gap-2 rounded-full border border-violet-200 bg-violet-50 px-3 py-1 text-xs text-violet-900 dark:border-violet-800 dark:bg-violet-950/40 dark:text-violet-200"
      data-testid="memory-saved-chip"
      data-layer={layer}
    >
      <Brain className="h-3.5 w-3.5 shrink-0" />
      <span className="shrink-0 font-medium">
        {undo === "undone" ? "Removed from memory" : "Saved to memory"} ·{" "}
        <span data-testid="memory-saved-chip-layer">{LAYER_LABELS[layer]}</span>
      </span>
      <span
        className={`min-w-0 truncate text-violet-800/80 dark:text-violet-200/80 ${undo === "undone" ? "line-through" : ""}`}
        title={memory}
      >
        {memory}
      </span>
      {canUndo && undo !== "undone" && (
        <button
          type="button"
          onClick={handleUndo}
          disabled={undo === "pending"}
          className="shrink-0 underline underline-offset-2 disabled:opacity-50"
          data-testid="memory-saved-chip-undo"
        >
          {undo === "error" ? "Undo failed, retry" : "Undo"}
        </button>
      )}
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
