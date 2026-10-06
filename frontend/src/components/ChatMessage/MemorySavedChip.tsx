import { useState } from "react"
import { Link, useLocation } from "react-router-dom"
import { Brain } from "lucide-react"
import { personalMemoryApi, workspaceMemoryApi } from "@/api/memory"
import { ApiError } from "@/api/client"
import type { MemoryLayer, SavedMemory } from "./savedMemory"

const LAYER_LABELS: Record<MemoryLayer, string> = {
  personal: "Personal",
  workspace: "Workspace",
}

type UndoState = "idle" | "pending" | "undone" | "error"

async function forget(layer: MemoryLayer, memoryId: string, workspaceId?: string) {
  if (layer === "personal") await personalMemoryApi.remove(memoryId)
  else if (workspaceId) await workspaceMemoryApi.remove(workspaceId, memoryId)
}

export function MemorySavedChip({
  layer,
  memory,
  memoryId,
  created,
  workspaceId,
}: SavedMemory & { workspaceId?: string }) {
  const location = useLocation()
  const prefix = location.pathname.startsWith("/embed") ? "/embed" : ""
  const [undo, setUndo] = useState<UndoState>("idle")
  // The agent saved it as this user, so they are its author and may delete it.
  const canUndo = created && !!memoryId && (layer === "personal" || !!workspaceId)

  const handleUndo = async () => {
    if (!memoryId || undo === "pending") return
    setUndo("pending")
    try {
      await forget(layer, memoryId, workspaceId)
      setUndo("undone")
    } catch (error) {
      // Both APIs 404 only for a memory that is already gone, which is what Undo wanted.
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
