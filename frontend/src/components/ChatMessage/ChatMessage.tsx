import { useState } from "react"
import type { UIMessage } from "ai"
import { isToolUIPart, getToolName } from "ai"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { useAppStore } from "@/store/store"
import { api } from "@/api/client"
import type { ActiveJob, RecentTermination } from "@/api/jobs"
import { MaterializationFailure } from "@/components/MaterializationStatus/MaterializationFailure"
import { Wrench, FileBarChart, Brain, ChevronDown, ChevronRight, Square } from "lucide-react"
import {
  QueryToolOutput,
  SemanticCatalogToolOutput,
  SemanticQueryToolOutput,
  DescribeTableOutput as DescribeTableOutputComponent,
  ListTablesOutput as ListTablesOutputComponent,
  GetMetadataOutput as GetMetadataOutputComponent,
} from "./ToolOutput"
import type {
  QueryOutput,
  SemanticCatalogOutput,
  SemanticQueryOutput,
  DescribeTableOutput,
  ListTablesOutput,
  GetMetadataOutput,
} from "./ToolOutput"

function parseOutput(output: unknown): unknown {
  if (typeof output === "string") {
    // The backend emits the MCP envelope as JSON (apps/chat/stream.py
    // _tool_content_to_str), so a plain JSON.parse is sufficient. The old
    // `output.replace(/'/g, '"')` Python-repr→JSON hack is vestigial under the
    // current adapter and actively corrupted any apostrophe in the data (05#2 /
    // 13#8), so it was removed.
    try {
      return JSON.parse(output)
    } catch {
      return output
    }
  }
  // Handle the MCP envelope array directly (already parsed objects)
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

function renderToolOutput(toolName: string, rawOutput: unknown): React.ReactNode | null {
  const output = parseOutput(rawOutput)
  if (output == null || typeof output !== "object") return null

  switch (toolName) {
    case "query":
      return <QueryToolOutput output={output as QueryOutput} />
    case "semantic_query":
      return <SemanticQueryToolOutput output={output as SemanticQueryOutput} />
    case "semantic_catalog":
    case "describe_dataset":
      return <SemanticCatalogToolOutput output={output as SemanticCatalogOutput} />
    case "describe_table":
      return <DescribeTableOutputComponent output={output as DescribeTableOutput} />
    case "list_tables":
      return <ListTablesOutputComponent output={output as ListTablesOutput} />
    case "get_metadata":
      return <GetMetadataOutputComponent output={output as GetMetadataOutput} />
    default:
      return null
  }
}

interface ChatMessageProps {
  message: UIMessage
  isActiveMessage: boolean
  workspaceId?: string
  threadId?: string
  activeMaterializationJob?: ActiveJob | null
  recentTerminationsByToolCallId?: Record<string, RecentTermination>
  onRetryDispatched?: () => void
}

interface ChatTextPartProps {
  role: "user" | "assistant"
  text: string
}

export function ChatTextPart({ role, text }: ChatTextPartProps) {
  const isUser = role === "user"
  return (
    <div
      className={`rounded-lg px-4 py-2 text-sm ${
        isUser
          ? "bg-primary text-primary-foreground"
          : "bg-muted prose prose-sm max-w-none"
      }`}
    >
      {isUser ? text : <Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown>}
    </div>
  )
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function isArtifactToolPart(part: any): boolean {
  const name = getToolName(part)
  if (name === "artifact_manager") return false
  if (name === "create_artifact" || name === "update_artifact") return true
  if (part.state === "output-available" && part.output != null) {
    const output = part.output
    if (typeof output === "string") return output.includes("artifact_id")
    if (typeof output === "object" && "artifact_id" in output) return true
  }
  return false
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function extractArtifactId(part: any): string | null {
  if (part.state !== "output-available" || part.output == null) return null
  const output = part.output
  if (typeof output === "object" && "artifact_id" in output) {
    return output.artifact_id as string
  }
  if (typeof output === "string") {
    const match = output.match(
      /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i,
    )
    return match ? match[0] : null
  }
  return null
}

function formatToolOutput(output: unknown): string {
  if (typeof output === "string") {
    // Try to parse JSON strings so we can pretty-print them
    try {
      const parsed = JSON.parse(output)
      if (typeof parsed === "object" && parsed !== null) {
        return JSON.stringify(parsed, null, 2)
      }
    } catch {
      // Not JSON — return as-is
    }
    return output
  }
  return JSON.stringify(output, null, 2)
}

// Tools that auto-expand to show their output.
// run_materialization is here because it emits MCP progress notifications.
// The data tools auto-expand because their rich output is the main value.
const AUTO_EXPAND_TOOLS = new Set([
  "run_materialization",
  "query",
  "semantic_query",
  "semantic_catalog",
  "describe_dataset",
  "describe_table",
  "list_tables",
  "get_metadata",
])

function displayToolName(toolName: string): string {
  if (toolName === "artifact_manager") return "Artifact Manager"
  return toolName
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function getSubagentToolData(part: any) {
  if (
    part?.type !== "data-subagent-tool-input"
    && part?.type !== "data-subagent-tool-output"
  ) {
    return null
  }
  const data = part.data
  if (
    data == null
    || typeof data !== "object"
    || typeof data.parentToolCallId !== "string"
    || typeof data.toolCallId !== "string"
    || typeof data.toolName !== "string"
  ) {
    return null
  }
  return data as {
    parentToolCallId: string
    subagentName?: string
    toolCallId: string
    toolName: string
    input?: unknown
    output?: unknown
  }
}

interface ToolCallPartProps {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  part: any
  index: number
  isLatest: boolean
  isActiveMessage: boolean
  workspaceId?: string
  threadId?: string
  activeMaterializationJob?: ActiveJob | null
  recentTermination?: RecentTermination | null
  onRetryDispatched?: () => void
  childParts?: any[]
  isNested?: boolean
}

export function ChatToolCallPart({ part, index, isLatest, isActiveMessage, workspaceId, threadId, activeMaterializationJob, recentTermination, onRetryDispatched, childParts = [], isNested = false }: ToolCallPartProps) {
  const toolName = getToolName(part)
  const isLoading = part.state === "input-streaming" || part.state === "input-available"
  const hasOutput = part.state === "output-available" || part.state === "output-error"
  const hasChildren = childParts.length > 0

  // Scope activeMaterializationJob to THIS specific tool-call card via
  // toolCallId — without this, the progress block and Stop button would
  // render on every historical run_materialization card in the thread.
  // part.toolCallId is the AI-SDK v6 field name surfaced on tool-input /
  // tool-output parts.
  const matchingJob =
    activeMaterializationJob
    && (activeMaterializationJob.tool_call_id === part.toolCallId)
      ? activeMaterializationJob
      : null

  // For run_materialization, prefer the live job. Only treat the termination
  // as a "show failure card" signal when there's no active job AND the
  // termination is FAILED/CANCELLED (we don't render a completed-state card —
  // the tool output handles success display).
  const matchingFailure =
    toolName === "run_materialization"
    && !matchingJob
    && recentTermination
    && (recentTermination.state === "failed" || recentTermination.state === "cancelled")
      ? recentTermination
      : null

  // Auto-expand while actively streaming; collapsed by default for historical messages.
  // User overrides tied to isLatest reset automatically when a part is superseded.
  // run_materialization stays expanded as long as there is an active job
  // FOR THIS CARD, regardless of whether the SSE stream is still active.
  // Also stay expanded when we have a failure card to show so the user can
  // see the error inline rather than having to expand a collapsed card.
  const autoExpanded =
    (toolName === "artifact_manager" && hasChildren)
    || (
      AUTO_EXPAND_TOOLS.has(toolName)
      && (
        isLatest
        || isLoading
        || (toolName === "run_materialization" && (!!matchingJob || !!matchingFailure))
      )
      && (isActiveMessage || toolName === "run_materialization")
    )
  const [override, setOverride] = useState<{ whenLatest: boolean; value: boolean } | null>(null)
  const effectiveOverride = override?.whenLatest === isLatest ? override.value : null
  const expanded = effectiveOverride ?? autoExpanded
  const toggleExpanded = () => setOverride({ whenLatest: isLatest, value: !expanded })

  const isErrored = part.state === "output-error"
  const richOutput =
    hasOutput && part.output != null && !isErrored ? renderToolOutput(toolName, part.output) : null
  // Fallback text for the <pre> view: an output-error part carries its message
  // in errorText (no `output`); otherwise show the raw output when no rich card
  // matched. Either way, the <pre> renders the FULL text — the historical
  // `.slice(0, 2000)` silently dropped the tail with no marker (13#4).
  const fallbackText = isErrored
    ? (part.errorText ?? "The tool reported an error.")
    : hasOutput && part.output != null && !richOutput
      ? formatToolOutput(part.output)
      : null

  const showCancelButton =
    toolName === "run_materialization"
    && !!matchingJob
    && (matchingJob.state === "pending" || matchingJob.state === "running")
    && !!workspaceId
  const [cancelState, setCancelState] = useState<"idle" | "pending" | "error">("idle")
  const handleCancel = async (e: React.MouseEvent) => {
    e.stopPropagation()
    if (!workspaceId || !matchingJob || cancelState === "pending") return
    setCancelState("pending")
    try {
      await api.post(
        `/api/workspaces/${workspaceId}/jobs/${matchingJob.thread_job_id}/cancel/`,
        {},
      )
    } catch {
      setCancelState("error")
      setTimeout(() => setCancelState("idle"), 3000)
    }
  }

  return (
    <div
      key={index}
      className={
        isNested
          ? "border-l border-border/70 pl-2 text-xs"
          : "rounded border bg-muted/30 my-1 text-xs"
      }
    >
      <div className="flex w-full items-center">
        <button
          type="button"
          onClick={toggleExpanded}
          className="flex flex-1 items-center gap-2 px-3 py-1.5 hover:bg-muted/50 transition-colors"
          data-testid={`tool-call-${toolName}`}
        >
          {expanded ? (
            <ChevronDown className="w-3 h-3 text-muted-foreground shrink-0" />
          ) : (
            <ChevronRight className="w-3 h-3 text-muted-foreground shrink-0" />
          )}
          <Wrench className="w-3 h-3 text-muted-foreground shrink-0" />
          <span className="text-muted-foreground">
            {displayToolName(toolName)}
            {isLoading && "..."}
          </span>
          {hasChildren && !isNested && (
            <span className="ml-auto rounded-sm bg-background px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {childParts.length} subagent {childParts.length === 1 ? "call" : "calls"}
            </span>
          )}
        </button>
        {showCancelButton && (
          <button
            type="button"
            onClick={handleCancel}
            disabled={cancelState === "pending"}
            className={`flex items-center gap-1 px-2 py-1 mr-1 rounded text-xs transition-colors ${
              cancelState === "error"
                ? "text-red-500"
                : "text-red-500/70 hover:text-red-500 hover:bg-red-500/10"
            }`}
            data-testid="materialization-cancel-btn"
            title={
              cancelState === "error"
                ? "Cancel failed — try again"
                : "Stop materialization"
            }
          >
            <Square className="w-3 h-3" />
            <span>{cancelState === "pending" ? "Cancelling..." : "Stop"}</span>
          </button>
        )}
      </div>
      {expanded && (
        hasChildren
        ||
        toolName === "run_materialization" && (matchingJob || matchingFailure)
        || richOutput
        || fallbackText
      ) && (
        <div className="border-t px-3 py-2.5">
          {hasChildren && (
            <div
              className="mb-2 space-y-1"
              data-testid={`tool-call-children-${toolName}`}
            >
              {childParts.map((childPart, childIndex) => (
                <ChatToolCallPart
                  key={(childPart as any).toolCallId ?? childIndex}
                  part={childPart}
                  index={childIndex}
                  isLatest={false}
                  isActiveMessage={false}
                  workspaceId={workspaceId}
                  threadId={threadId}
                  onRetryDispatched={onRetryDispatched}
                  isNested
                />
              ))}
            </div>
          )}
          {toolName === "run_materialization" && matchingJob && (
            <div className="text-xs text-muted-foreground mb-2">
              ⏳ {matchingJob.progress?.message ?? "Materializing..."}
              {matchingJob.progress?.rows_loaded != null && (
                <>
                  {" "}({matchingJob.progress.rows_loaded.toLocaleString()}
                  {matchingJob.progress.rows_total
                    ? ` / ${matchingJob.progress.rows_total.toLocaleString()}`
                    : ""})
                </>
              )}
              {matchingJob.progress?.percent != null && (
                <> — {matchingJob.progress.percent}%</>
              )}
            </div>
          )}
          {toolName === "run_materialization"
            && matchingFailure
            && workspaceId
            && threadId && (
              <MaterializationFailure
                termination={matchingFailure}
                workspaceId={workspaceId}
                threadId={threadId}
                onRetryDispatched={onRetryDispatched}
              />
            )}
          {richOutput ?? (
            fallbackText && (
              // No client-side slice: the scroll container caps the height and
              // the backend already truncates with an explicit marker
              // (apps/chat/stream.py), so the old `.slice(0, 2000)` only dropped
              // the tail silently (13#4).
              <pre className="whitespace-pre-wrap text-xs text-muted-foreground font-mono max-h-60 overflow-auto">
                {fallbackText}
              </pre>
            )
          )}
        </div>
      )}
    </div>
  )
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function ChatReasoningPart({ part, index, isLatest, isActiveMessage }: { part: any; index: number; isLatest: boolean; isActiveMessage: boolean }) {
  const text = part.reasoning || part.text || ""

  // Only auto-expand while actively streaming. On historical loads or once superseded: collapsed.
  const autoExpanded = isActiveMessage && isLatest
  const [override, setOverride] = useState<{ whenLatest: boolean; value: boolean } | null>(null)
  const effectiveOverride = override?.whenLatest === isLatest ? override.value : null
  const expanded = effectiveOverride ?? autoExpanded
  const toggleExpanded = () => setOverride({ whenLatest: isLatest, value: !expanded })

  if (!text) return null

  return (
    <div key={index} className="rounded border border-dashed bg-muted/20 my-1 text-xs">
      <button
        type="button"
        onClick={toggleExpanded}
        className="flex w-full items-center gap-2 px-3 py-1.5 hover:bg-muted/50 transition-colors"
        data-testid="thinking-toggle"
      >
        {expanded ? (
          <ChevronDown className="w-3 h-3 text-muted-foreground shrink-0" />
        ) : (
          <ChevronRight className="w-3 h-3 text-muted-foreground shrink-0" />
        )}
        <Brain className="w-3 h-3 text-purple-500 shrink-0" />
        <span className="text-muted-foreground">Thinking</span>
      </button>
      {expanded && (
        <div className="border-t px-3 py-2 max-h-80 overflow-auto">
          <div className="text-xs text-muted-foreground whitespace-pre-wrap font-mono">
            {text}
          </div>
        </div>
      )}
    </div>
  )
}

interface ChatArtifactButtonProps {
  artifactId: string
  isActive?: boolean
  onOpen?: (artifactId: string) => void
}

export function ChatArtifactButton({ artifactId, isActive = false, onOpen }: ChatArtifactButtonProps) {
  return (
    <button
      onClick={() => onOpen?.(artifactId)}
      className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm my-1 transition-colors hover:bg-muted ${
        isActive
          ? "border-primary bg-primary/5"
          : "border-border"
      }`}
    >
      <FileBarChart className="h-4 w-4 text-primary" />
      <span>View Artifact</span>
    </button>
  )
}

export function ChatMessage({ message, isActiveMessage, workspaceId, threadId, activeMaterializationJob, recentTerminationsByToolCallId, onRetryDispatched }: ChatMessageProps) {
  const isUser = message.role === "user"
  const activeArtifactId = useAppStore((s) => s.activeArtifactId)
  const openArtifact = useAppStore((s) => s.uiActions.openArtifact)
  const childToolPartsByParent = new Map<string, any[]>()
  const childToolPartsById = new Map<string, any>()

  for (const part of message.parts) {
    if (isToolUIPart(part)) {
      const parentToolCallId = (part as any).parentToolCallId
      if (typeof parentToolCallId !== "string" || !parentToolCallId) continue
      const children = childToolPartsByParent.get(parentToolCallId) ?? []
      children.push(part)
      childToolPartsByParent.set(parentToolCallId, children)
      continue
    }

    const subagentData = getSubagentToolData(part)
    if (!subagentData) continue
    const child =
      childToolPartsById.get(subagentData.toolCallId)
      ?? {
        type: `tool-${subagentData.toolName}`,
        toolName: subagentData.toolName,
        toolCallId: subagentData.toolCallId,
        parentToolCallId: subagentData.parentToolCallId,
        subagentName: subagentData.subagentName,
        state: "input-available",
        input: {},
      }
    child.toolName = subagentData.toolName
    child.type = `tool-${subagentData.toolName}`
    child.parentToolCallId = subagentData.parentToolCallId
    child.subagentName = subagentData.subagentName
    if (part.type === "data-subagent-tool-input") {
      child.input = subagentData.input ?? {}
      if (child.state !== "output-available") child.state = "input-available"
    } else {
      child.output = subagentData.output
      child.state = "output-available"
    }
    if (!childToolPartsById.has(subagentData.toolCallId)) {
      childToolPartsById.set(subagentData.toolCallId, child)
      const children = childToolPartsByParent.get(subagentData.parentToolCallId) ?? []
      children.push(child)
      childToolPartsByParent.set(subagentData.parentToolCallId, children)
    }
  }

  return (
    <div className={`flex w-full ${isUser ? "justify-end" : "justify-start"}`}>
      <div className="max-w-[90%]">
        {message.parts.map((part, i) => {
          if (part.type === "text") {
            return <ChatTextPart key={i} role={isUser ? "user" : "assistant"} text={part.text} />
          }

          if (part.type === "reasoning") {
            return <ChatReasoningPart key={i} part={part} index={i} isLatest={i === message.parts.length - 1} isActiveMessage={isActiveMessage} />
          }

          if (isToolUIPart(part)) {
            const parentToolCallId = (part as any).parentToolCallId
            if (typeof parentToolCallId === "string" && parentToolCallId) {
              return null
            }
            if (isArtifactToolPart(part)) {
              const artifactId = extractArtifactId(part)
              if (artifactId && part.state === "output-available") {
                const isActive = activeArtifactId === artifactId
                return (
                  <ChatArtifactButton
                    key={i}
                    artifactId={artifactId}
                    isActive={isActive}
                    onOpen={openArtifact}
                  />
                )
              }
            }

            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const toolCallId = (part as any).toolCallId
            const recentTermination =
              toolCallId && recentTerminationsByToolCallId
                ? recentTerminationsByToolCallId[toolCallId] ?? null
                : null
            return <ChatToolCallPart key={i} part={part} index={i} isLatest={i === message.parts.length - 1} isActiveMessage={isActiveMessage} workspaceId={workspaceId} threadId={threadId} activeMaterializationJob={activeMaterializationJob} recentTermination={recentTermination} onRetryDispatched={onRetryDispatched} childParts={toolCallId ? childToolPartsByParent.get(toolCallId) ?? [] : []} />
          }

          return null
        })}
      </div>
    </div>
  )
}
