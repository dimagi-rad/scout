import { useState } from "react"
import type { UIMessage } from "ai"
import { isToolUIPart, getToolName } from "ai"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { useAppStore } from "@/store/store"
import { Bot, User, Wrench, FileBarChart, Brain, ChevronDown, ChevronRight } from "lucide-react"

interface ChatMessageProps {
  message: UIMessage
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function isArtifactToolPart(part: any): boolean {
  const name = getToolName(part)
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

// Tools that emit MCP progress notifications and should auto-expand.
// Mirrors _PROGRESS_TOOLS in apps/chat/stream.py — keep in sync when adding new tools.
const PROGRESS_TOOLS = new Set(["run_materialization"])

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function ToolCallPart({ part, index }: { part: any; index: number }) {
  const toolName = getToolName(part)
  const [expanded, setExpanded] = useState(() => PROGRESS_TOOLS.has(toolName))
  const isLoading = part.state === "input-streaming" || part.state === "input-available"
  const hasOutput = part.state === "output-available" || part.state === "output-error"
  const outputText = hasOutput && part.output != null
    ? formatToolOutput(part.output)
    : null

  return (
    <div key={index} className="rounded border bg-muted/30 my-1 text-xs">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center gap-2 px-3 py-1.5 hover:bg-muted/50 transition-colors"
        data-testid={`tool-call-${toolName}`}
      >
        {expanded ? (
          <ChevronDown className="w-3 h-3 text-muted-foreground shrink-0" />
        ) : (
          <ChevronRight className="w-3 h-3 text-muted-foreground shrink-0" />
        )}
        <Wrench className="w-3 h-3 text-muted-foreground shrink-0" />
        <span className="text-muted-foreground">
          {toolName}
          {isLoading && "..."}
        </span>
      </button>
      {expanded && outputText && (
        <div className="border-t px-3 py-2 max-h-60 overflow-auto">
          <pre className="whitespace-pre-wrap text-xs text-muted-foreground font-mono">
            {outputText.slice(0, 2000)}
          </pre>
        </div>
      )}
    </div>
  )
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function ReasoningPart({ part, index }: { part: any; index: number }) {
  const [expanded, setExpanded] = useState(false)
  const text = part.reasoning || part.text || ""

  if (!text) return null

  return (
    <div key={index} className="rounded border border-dashed bg-muted/20 my-1 text-xs">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
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

export function ChatMessage({ message }: ChatMessageProps) {
  const isUser = message.role === "user"
  const activeArtifactId = useAppStore((s) => s.activeArtifactId)
  const openArtifact = useAppStore((s) => s.uiActions.openArtifact)

  return (
    <div className={`flex gap-3 ${isUser ? "justify-end" : ""}`}>
      {!isUser && (
        <div className="flex-shrink-0 w-8 h-8 rounded-full bg-primary/10 flex items-center justify-center">
          <Bot className="w-4 h-4 text-primary" />
        </div>
      )}

      <div className={`max-w-[80%] ${isUser ? "order-first" : ""}`}>
        {message.parts.map((part, i) => {
          if (part.type === "text") {
            return (
              <div
                key={i}
                className={`rounded-lg px-4 py-2 text-sm ${
                  isUser
                    ? "bg-primary text-primary-foreground"
                    : "bg-muted prose prose-sm max-w-none"
                }`}
              >
                {isUser ? (
                  part.text
                ) : (
                  <Markdown remarkPlugins={[remarkGfm]}>{part.text}</Markdown>
                )}
              </div>
            )
          }

          if (part.type === "reasoning") {
            return <ReasoningPart key={i} part={part} index={i} />
          }

          if (isToolUIPart(part)) {
            if (isArtifactToolPart(part)) {
              const artifactId = extractArtifactId(part)
              if (artifactId && part.state === "output-available") {
                const isActive = activeArtifactId === artifactId
                return (
                  <button
                    key={i}
                    onClick={() => openArtifact(artifactId)}
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
            }

            return <ToolCallPart key={i} part={part} index={i} />
          }

          return null
        })}
      </div>

      {isUser && (
        <div className="flex-shrink-0 w-8 h-8 rounded-full bg-primary flex items-center justify-center">
          <User className="w-4 h-4 text-primary-foreground" />
        </div>
      )}
    </div>
  )
}
