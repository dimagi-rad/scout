import { parseOutput } from "./artifactReferences"

export const MEMORY_TOOL_LAYERS: Record<string, MemoryLayer> = {
  save_personal_memory: "personal",
  save_workspace_memory: "workspace",
}

export type MemoryLayer = "personal" | "workspace"

export interface SavedMemory {
  layer: MemoryLayer
  memory: string
  memoryId?: string
  // False when the memory already existed, so undoing it would delete an older save.
  created: boolean
}

/** The saved memory a finished memory tool call reports, or null for a failure or denial. */
export function savedMemory(toolName: string, output: unknown): SavedMemory | null {
  const fallbackLayer = MEMORY_TOOL_LAYERS[toolName]
  if (!fallbackLayer) return null
  const parsed = parseOutput(output)
  if (parsed == null || typeof parsed !== "object") return null
  const { status, layer, memory, memory_id: memoryId } = parsed as Record<string, unknown>
  if (status !== "saved" && status !== "already_saved") return null
  if (typeof memory !== "string" || !memory) return null
  return {
    layer: layer === "personal" || layer === "workspace" ? layer : fallbackLayer,
    memory,
    memoryId: typeof memoryId === "string" ? memoryId : undefined,
    created: status === "saved",
  }
}
