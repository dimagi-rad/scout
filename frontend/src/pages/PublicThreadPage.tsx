import { useState, useEffect } from "react"
import { parseShareToken, shareApiUrl } from "@/lib/shareToken"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Bot, User, Wrench, FileBarChart } from "lucide-react"

interface MessagePart {
  type: string
  text?: string
  toolCallId?: string
  toolName?: string
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  input?: any
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  output?: any
  state?: string
}

interface Message {
  id: string
  role: "user" | "assistant"
  parts: MessagePart[]
}

interface Artifact {
  id: string
  title: string
  artifact_type: string
  code: string
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  data: any
  version: number
}

interface SharedThread {
  thread: {
    id: string
    title: string
    created_at: string
  }
  messages: Message[]
  artifacts: Artifact[]
}

function formatDateTime(dateString: string): string {
  return new Date(dateString).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })
}

function getTokenFromPath(): string | undefined {
  return parseShareToken(window.location.pathname, "threads")
}

function isArtifactTool(part: MessagePart): boolean {
  return part.toolName === "create_artifact" || part.toolName === "update_artifact"
}

function extractArtifactId(part: MessagePart): string | null {
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

function PublicChatMessage({
  message,
  artifacts,
  activeArtifactId,
  onSelectArtifact,
}: {
  message: Message
  artifacts: Artifact[]
  activeArtifactId: string | null
  onSelectArtifact: (id: string) => void
}) {
  const isUser = message.role === "user"

  return (
    <div className={`flex gap-3 ${isUser ? "justify-end" : ""}`}>
      {!isUser && (
        <div className="flex-shrink-0 w-8 h-8 rounded-full bg-primary/10 flex items-center justify-center">
          <Bot className="w-4 h-4 text-primary" />
        </div>
      )}

      <div className={`max-w-[80%] ${isUser ? "order-first" : ""}`}>
        {message.parts.map((part, i) => {
          if (part.type === "text" && part.text) {
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

          if (part.type?.startsWith("tool-") || part.type === "dynamic-tool") {
            if (isArtifactTool(part)) {
              const artifactId = extractArtifactId(part)
              const artifact = artifactId
                ? artifacts.find((a) => a.id === artifactId)
                : null
              if (artifact) {
                const isActive = activeArtifactId === artifact.id
                return (
                  <button
                    key={i}
                    onClick={() => onSelectArtifact(artifact.id)}
                    className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm my-1 transition-colors hover:bg-muted ${
                      isActive
                        ? "border-primary bg-primary/5"
                        : "border-border"
                    }`}
                  >
                    <FileBarChart className="h-4 w-4 text-primary" />
                    <span>{artifact.title || "View Artifact"}</span>
                  </button>
                )
              }
            }

            return (
              <div key={i} className="rounded border bg-muted/30 my-1 text-xs">
                <div className="flex items-center gap-2 px-3 py-1.5">
                  <Wrench className="w-3 h-3 text-muted-foreground" />
                  <span className="text-muted-foreground">{part.toolName}</span>
                </div>
              </div>
            )
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

// Self-contained markup renderable directly in a sandboxed iframe. React/Plotly
// artifacts need the CDN-backed renderer plus auth'd query data, so anonymous
// viewers see their source instead.
const SANDBOXABLE_TYPES = new Set(["html", "svg"])

function buildSandboxSrcDoc(artifact: Artifact): string {
  const body =
    artifact.artifact_type === "svg"
      ? `<div style="display:flex;align-items:center;justify-content:center;min-height:100%">${artifact.code}</div>`
      : artifact.code
  return `<!DOCTYPE html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:12px;font-family:system-ui,-apple-system,sans-serif}</style></head><body>${body}</body></html>`
}

function ArtifactPreview({ artifact }: { artifact: Artifact }) {
  return (
    <Card data-testid={`public-artifact-${artifact.id}`}>
      <CardHeader>
        <CardTitle className="text-base">{artifact.title}</CardTitle>
        <span className="text-xs text-muted-foreground">{artifact.artifact_type}</span>
      </CardHeader>
      <CardContent>
        {artifact.artifact_type === "markdown" ? (
          <div className="prose prose-sm dark:prose-invert max-w-none">
            <Markdown remarkPlugins={[remarkGfm]}>{artifact.code}</Markdown>
          </div>
        ) : SANDBOXABLE_TYPES.has(artifact.artifact_type) ? (
          <iframe
            // SECURITY: render untrusted, agent-generated markup with NO
            // allow-same-origin so the frame gets a unique opaque origin and
            // cannot touch the parent or this origin's cookies. allow-scripts
            // is intentionally omitted: static html/svg need no JS, and
            // withholding it blocks script execution entirely.
            sandbox=""
            srcDoc={buildSandboxSrcDoc(artifact)}
            title={artifact.title}
            className="h-96 w-full rounded border bg-white"
            data-testid={`public-artifact-frame-${artifact.id}`}
          />
        ) : (
          <pre
            className="max-h-96 overflow-auto rounded bg-muted p-3 text-xs font-mono whitespace-pre-wrap"
            data-testid={`public-artifact-code-${artifact.id}`}
          >
            {artifact.code}
          </pre>
        )}
      </CardContent>
    </Card>
  )
}

export function PublicThreadPage() {
  const token = getTokenFromPath()
  const [data, setData] = useState<SharedThread | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [activeArtifactId, setActiveArtifactId] = useState<string | null>(null)

  useEffect(() => {
    if (!token) return
    fetch(shareApiUrl("threads", token))
      .then((res) => {
        if (!res.ok) throw new Error(res.status === 404 ? "Thread not found" : "Failed to load thread")
        return res.json()
      })
      .then(setData)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [token])

  if (loading) {
    return (
      <div className="mx-auto max-w-4xl p-6 space-y-6">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-4 w-48" />
        <Skeleton className="h-60 w-full" />
      </div>
    )
  }

  if (error || !data) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Card className="w-full max-w-md">
          <CardContent className="pt-6 text-center">
            <p className="text-lg font-medium text-destructive">{error ?? "Thread not found"}</p>
            <p className="mt-2 text-sm text-muted-foreground">
              This conversation may have been removed or the link may be invalid.
            </p>
          </CardContent>
        </Card>
      </div>
    )
  }

  const activeArtifact = activeArtifactId
    ? data.artifacts.find((a) => a.id === activeArtifactId) ?? null
    : null

  return (
    <div className="mx-auto max-w-4xl p-6 space-y-6">
      <div>
        <p className="text-xs text-muted-foreground uppercase tracking-wide mb-1">
          Shared Conversation
        </p>
        <h1 className="text-2xl font-bold">{data.thread.title}</h1>
        <p className="mt-1 text-xs text-muted-foreground">
          {formatDateTime(data.thread.created_at)}
        </p>
      </div>

      <div className="flex gap-6">
        <div className="flex-1 space-y-4">
          {data.messages.length === 0 ? (
            <p className="text-sm text-muted-foreground">No messages in this conversation.</p>
          ) : (
            data.messages.map((msg) => (
              <PublicChatMessage
                key={msg.id}
                message={msg}
                artifacts={data.artifacts}
                activeArtifactId={activeArtifactId}
                onSelectArtifact={setActiveArtifactId}
              />
            ))
          )}
        </div>

        {activeArtifact && (
          <div className="w-96 shrink-0">
            <div className="sticky top-6">
              <ArtifactPreview artifact={activeArtifact} />
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
