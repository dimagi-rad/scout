import type { UIMessage } from "ai"
import { isToolUIPart } from "ai"

interface ArtifactToolPart {
  state?: string
  output?: unknown
  parentToolCallId?: string
}

export interface MessageArtifact {
  id: string
  version?: number
  afterIndex: number
}

export function parseOutput(output: unknown): unknown {
  if (typeof output === "string") {
    // Backend emits the MCP envelope as JSON (apps/chat/stream.py), so a plain
    // JSON.parse suffices. The old Python-repr→JSON `replace(/'/g, '"')` hack
    // corrupted apostrophes in the data (05#2 / 13#8) and was removed.
    try {
      return JSON.parse(output)
    } catch {
      return output
    }
  }
  // MCP envelope array (already-parsed objects).
  if (
    Array.isArray(output) &&
    output[0]?.type === "text" &&
    typeof output[0]?.text === "string"
  ) {
    try {
      return JSON.parse(output[0].text)
    } catch {
      return output
    }
  }
  return output
}

export function isFailedOutput(output: unknown): boolean {
  return output !== null && typeof output === "object" && "status" in output
    && typeof output.status === "string"
    && ["error", "denied"].includes(output.status)
}

export function isUnsuccessfulHelperOutcome(output: unknown): boolean {
  // A data-model handoff can follow a published write: keep its card open
  // without suppressing the link to the artifact that was already created.
  return isFailedOutput(output) || (
    output !== null && typeof output === "object" && "status" in output
    && typeof output.status === "string"
    && ["blocked", "needs_data_model", "invalid_data_requirements"].includes(output.status)
  )
}

export function getSubagentToolData(part: { type: string; data?: unknown }) {
  if (part.type !== "data-subagent-tool-input" && part.type !== "data-subagent-tool-output") return null
  const data = part.data
  if (data === null || typeof data !== "object"
    || !("parentToolCallId" in data) || typeof data.parentToolCallId !== "string"
    || !("toolCallId" in data) || typeof data.toolCallId !== "string"
    || !("toolName" in data) || typeof data.toolName !== "string") return null
  return data as {
    parentToolCallId: string
    subagentName?: string
    toolCallId: string
    toolName: string
    input?: unknown
    output?: unknown
  }
}

export function extractArtifactIdFromOutput(rawOutput: unknown): string | null {
  const output = parseOutput(rawOutput)
  if (output == null) return null
  if (isFailedOutput(output)) return null
  if (typeof output === "object" && !Array.isArray(output)) {
    if (
      "artifact_id" in output
      && typeof output.artifact_id === "string"
      && output.artifact_id
    ) {
      return output.artifact_id
    }
    if (
      "artifact" in output
      && output.artifact != null
      && typeof output.artifact === "object"
      && "id" in output.artifact
      && typeof output.artifact.id === "string"
      && output.artifact.id
    ) {
      return output.artifact.id
    }
  }
  return null
}

export function messageArtifacts(message: UIMessage): Map<string, MessageArtifact> {
  const artifacts = new Map<string, MessageArtifact>()
  const parentIndices = new Map<string, number>()
  message.parts.forEach((part, index) => {
    if (isToolUIPart(part) && !(part as ArtifactToolPart).parentToolCallId) {
      parentIndices.set(part.toolCallId, index)
    }
  })
  message.parts.forEach((part, index) => {
    const child = part.type === "data-subagent-tool-output" ? getSubagentToolData(part) : null
    if (part.type === "data-subagent-tool-output" && !child) return
    const tool = isToolUIPart(part) ? part as ArtifactToolPart : null
    if (tool?.state !== "output-available" && part.type !== "data-subagent-tool-output") return
    const rawOutput = child?.output ?? tool?.output
    const output = parseOutput(rawOutput) as { artifact_version?: unknown; artifact?: { version?: unknown } }
    const id = extractArtifactIdFromOutput(output)
    if (!id) return
    const rawVersion = output.artifact_version ?? output.artifact?.version
    const version = typeof rawVersion === "number" ? rawVersion : undefined
    const parentId = child?.parentToolCallId ?? tool?.parentToolCallId
    const afterIndex = parentId ? parentIndices.get(parentId) : index
    if (afterIndex === undefined) return
    const previous = artifacts.get(id)
    if (previous && (previous.version ?? 0) > (version ?? 0)) return
    artifacts.set(id, { id, version, afterIndex })
  })
  return artifacts
}

export function turnArtifactOwners(messages: UIMessage[]): Map<string, ReadonlySet<string>> {
  const visible = new Map<string, Set<string>>()
  let turn = new Map<string, { messageId: string; version?: number }>()
  const finishTurn = () => {
    for (const [id, artifact] of turn) visible.get(artifact.messageId)?.add(id)
    turn = new Map()
  }
  for (const message of messages) {
    visible.set(message.id, new Set())
    if (message.role === "user") {
      finishTurn()
      continue
    }
    for (const artifact of messageArtifacts(message).values()) {
      const previous = turn.get(artifact.id)
      if (previous && (previous.version ?? 0) > (artifact.version ?? 0)) continue
      turn.set(artifact.id, { messageId: message.id, version: artifact.version })
    }
  }
  finishTurn()
  return visible
}
