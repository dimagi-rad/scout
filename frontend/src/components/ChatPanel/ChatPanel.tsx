import { useChat } from "@ai-sdk/react"
import { DefaultChatTransport, type UIMessage } from "ai"
import { useEffect, useRef, useState } from "react"
import { getCsrfToken, api } from "@/api/client"
import { useAppStore } from "@/store/store"
import { ChatMessage } from "@/components/ChatMessage/ChatMessage"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Send, Square } from "lucide-react"

function threadStorageKey(projectId: string) {
  return `scout:thread:${projectId}`
}

export function ChatPanel() {
  const activeProjectId = useAppStore((s) => s.activeProjectId)
  const threadId = useAppStore((s) => s.threadId)
  const selectThread = useAppStore((s) => s.uiActions.selectThread)
  const fetchThreads = useAppStore((s) => s.uiActions.fetchThreads)
  const scrollRef = useRef<HTMLDivElement>(null)
  const [input, setInput] = useState("")
  const prevStatusRef = useRef<string>("")

  // Use a ref so the transport body closure always reads fresh values,
  // even though useChat caches the transport from the first render.
  const contextRef = useRef({ projectId: activeProjectId, threadId })
  contextRef.current = { projectId: activeProjectId, threadId }

  const [transport] = useState(
    () =>
      new DefaultChatTransport({
        api: "/api/chat/",
        credentials: "include",
        headers: () => ({ "X-CSRFToken": getCsrfToken() }),
        body: () => ({ data: contextRef.current }),
      }),
  )

  const { messages, sendMessage, status, stop, error, setMessages } = useChat({
    transport,
  })

  const isStreaming = status === "streaming" || status === "submitted"

  // Persist threadId to localStorage when it changes
  useEffect(() => {
    if (activeProjectId) {
      localStorage.setItem(threadStorageKey(activeProjectId), threadId)
    }
  }, [threadId, activeProjectId])

  // Restore threadId from localStorage when project changes
  useEffect(() => {
    if (!activeProjectId) return
    const saved = localStorage.getItem(threadStorageKey(activeProjectId))
    if (saved && saved !== threadId) {
      selectThread(saved)
    }
    // Only run when project changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeProjectId])

  // Load messages from backend when threadId changes
  useEffect(() => {
    if (!threadId) return
    let cancelled = false

    async function loadMessages() {
      try {
        const msgs = await api.get<UIMessage[]>(`/api/chat/threads/${threadId}/messages/`)
        if (!cancelled) {
          setMessages(msgs)
        }
      } catch {
        // New thread or fetch failed — start with empty
        if (!cancelled) {
          setMessages([])
        }
      }
    }

    loadMessages()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId])

  // Refresh thread list when streaming finishes (so new threads appear)
  useEffect(() => {
    if (prevStatusRef.current === "streaming" && status === "ready" && activeProjectId) {
      fetchThreads(activeProjectId)
    }
    prevStatusRef.current = status
  }, [status, activeProjectId, fetchThreads])

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [messages])

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const text = input.trim()
    if (!text || isStreaming) return
    setInput("")
    sendMessage({ text })
  }

  if (!activeProjectId) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted-foreground">
        Select a project to start chatting.
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      {/* Message list */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.length === 0 && (
          <div className="text-center text-muted-foreground mt-20">
            Ask a question about your data to get started.
          </div>
        )}
        {messages.map((msg: UIMessage) => (
          <ChatMessage key={msg.id} message={msg} />
        ))}
        {error && (
          <div className="text-sm text-destructive bg-destructive/10 rounded-lg px-4 py-2">
            {error.message}
          </div>
        )}
      </div>

      {/* Input area */}
      <div className="border-t p-4">
        <form onSubmit={handleSubmit} className="flex gap-2">
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask about your data..."
            disabled={isStreaming}
            className="flex-1"
          />
          {isStreaming ? (
            <Button type="button" variant="outline" size="icon" onClick={() => stop()}>
              <Square className="w-4 h-4" />
            </Button>
          ) : (
            <Button type="submit" size="icon" disabled={!input.trim()}>
              <Send className="w-4 h-4" />
            </Button>
          )}
        </form>
      </div>
    </div>
  )
}
