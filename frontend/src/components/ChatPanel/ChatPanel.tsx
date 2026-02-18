import { useChat } from "@ai-sdk/react"
import { DefaultChatTransport, type UIMessage } from "ai"
import { useCallback, useEffect, useRef, useState } from "react"
import { getCsrfToken, api } from "@/api/client"
import { useAppStore } from "@/store/store"
import { ChatMessage } from "@/components/ChatMessage/ChatMessage"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Send, Square, Share2, Users, Globe, Link, Copy, Check } from "lucide-react"
import { SLASH_COMMANDS } from "./slashCommands"
import { SlashCommandMenu } from "./SlashCommandMenu"

function threadStorageKey(domainId: string) {
  return `scout:thread:${domainId}`
}

function getPublicUrl(token: string): string {
  return `${window.location.origin}/shared/threads/${token}/`
}

function ShareMenu({
  threadId,
  onClose,
}: {
  threadId: string
  onClose: () => void
}) {
  const threads = useAppStore((s) => s.threads)
  const updateThreadSharing = useAppStore((s) => s.uiActions.updateThreadSharing)
  const thread = threads.find((t) => t.id === threadId)
  const [copied, setCopied] = useState(false)

  const isShared = thread?.is_shared ?? false
  const isPublic = thread?.is_public ?? false
  const shareToken = thread?.share_token ?? null

  const handleCopy = useCallback(async () => {
    if (!shareToken) return
    await navigator.clipboard.writeText(getPublicUrl(shareToken))
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }, [shareToken])

  return (
    <div className="absolute right-0 top-full mt-1 z-50 w-72 rounded-lg border bg-popover p-3 shadow-md" data-testid="share-menu">
      <div className="space-y-3">
        <label
          className="flex items-center gap-2 cursor-pointer text-sm"
          data-testid="thread-sharing-project"
        >
          <input
            type="checkbox"
            checked={isShared}
            onChange={(e) => updateThreadSharing(threadId, { is_shared: e.target.checked })}
            className="h-4 w-4 rounded border-gray-300"
          />
          <Users className="h-4 w-4 text-muted-foreground" />
          <span>Share with project</span>
        </label>
        <label
          className="flex items-center gap-2 cursor-pointer text-sm"
          data-testid="thread-sharing-public"
        >
          <input
            type="checkbox"
            checked={isPublic}
            onChange={(e) => updateThreadSharing(threadId, { is_public: e.target.checked })}
            className="h-4 w-4 rounded border-gray-300"
          />
          <Globe className="h-4 w-4 text-muted-foreground" />
          <span>Public link</span>
        </label>
        {isPublic && shareToken && (
          <div
            className="flex items-center gap-2 rounded-md border bg-muted/50 p-2"
            data-testid="thread-share-url"
          >
            <Link className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            <code className="flex-1 truncate text-xs">
              {getPublicUrl(shareToken)}
            </code>
            <Button
              variant="outline"
              size="sm"
              onClick={handleCopy}
              data-testid="copy-thread-share-link"
              className="h-7 px-2"
            >
              {copied ? (
                <Check className="h-3 w-3" />
              ) : (
                <Copy className="h-3 w-3" />
              )}
            </Button>
          </div>
        )}
      </div>
      <button
        type="button"
        onClick={onClose}
        className="absolute -top-2 -right-2 rounded-full border bg-background p-1 text-muted-foreground hover:text-foreground"
      >
        <span className="sr-only">Close</span>
        <svg className="h-3 w-3" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M2 2l8 8M10 2l-8 8" />
        </svg>
      </button>
    </div>
  )
}

export function ChatPanel() {
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const threadId = useAppStore((s) => s.threadId)
  const selectThread = useAppStore((s) => s.uiActions.selectThread)
  const fetchThreads = useAppStore((s) => s.uiActions.fetchThreads)
  const scrollRef = useRef<HTMLDivElement>(null)
  const [input, setInput] = useState("")
  const [slashMenuIndex, setSlashMenuIndex] = useState(0)
  const [showShareMenu, setShowShareMenu] = useState(false)
  const prevStatusRef = useRef<string>("")

  // Use a ref so the transport body closure always reads fresh values,
  // even though useChat caches the transport from the first render.
  const contextRef = useRef({ tenantId: activeDomainId, threadId })
  contextRef.current = { tenantId: activeDomainId, threadId }

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

  // Slash command menu state
  const showSlashMenu =
    !isStreaming && input.startsWith("/") && !input.slice(1).includes(" ")
  const slashQuery = showSlashMenu ? input.slice(1) : ""
  const filteredCommands = SLASH_COMMANDS.filter((cmd) =>
    cmd.name.startsWith(slashQuery),
  )

  function selectSlashCommand(cmd: typeof SLASH_COMMANDS[number]) {
    setInput(`/${cmd.name} `)
    setSlashMenuIndex(0)
  }

  // Persist threadId to localStorage when it changes
  useEffect(() => {
    if (activeDomainId) {
      localStorage.setItem(threadStorageKey(activeDomainId), threadId)
    }
  }, [threadId, activeDomainId])

  // Restore threadId from localStorage when domain changes
  useEffect(() => {
    if (!activeDomainId) return
    const saved = localStorage.getItem(threadStorageKey(activeDomainId))
    if (saved && saved !== threadId) {
      selectThread(saved)
    }
    // Only run when domain changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDomainId])

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
    if (prevStatusRef.current === "streaming" && status === "ready" && activeDomainId) {
      fetchThreads(activeDomainId)
    }
    prevStatusRef.current = status
  }, [status, activeDomainId, fetchThreads])

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

    if (text.startsWith("/")) {
      const spaceIdx = text.indexOf(" ")
      const cmdName = spaceIdx === -1 ? text.slice(1) : text.slice(1, spaceIdx)
      const args = spaceIdx === -1 ? "" : text.slice(spaceIdx + 1).trim()
      const cmd = SLASH_COMMANDS.find((c) => c.name === cmdName)
      if (cmd) {
        setInput("")
        sendMessage({ text: cmd.buildPrompt(args) })
        return
      }
    }

    setInput("")
    sendMessage({ text })
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (!showSlashMenu || filteredCommands.length === 0) return

    if (e.key === "ArrowDown") {
      e.preventDefault()
      setSlashMenuIndex((i) => (i + 1) % filteredCommands.length)
    } else if (e.key === "ArrowUp") {
      e.preventDefault()
      setSlashMenuIndex((i) => (i - 1 + filteredCommands.length) % filteredCommands.length)
    } else if (e.key === "Tab" || e.key === "Enter") {
      e.preventDefault()
      selectSlashCommand(filteredCommands[slashMenuIndex])
    }
  }

  if (!activeDomainId) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted-foreground">
        Select a domain to start chatting
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header with share */}
      {messages.length > 0 && (
        <div className="flex items-center justify-end border-b px-4 py-2">
          <div className="relative">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setShowShareMenu(!showShareMenu)}
              data-testid="chat-share-btn"
            >
              <Share2 className="mr-1 h-4 w-4" />
              Share
            </Button>
            {showShareMenu && (
              <ShareMenu
                threadId={threadId}
                onClose={() => setShowShareMenu(false)}
              />
            )}
          </div>
        </div>
      )}

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
        <form onSubmit={handleSubmit} className="relative flex gap-2">
          <SlashCommandMenu
            query={slashQuery}
            onSelect={selectSlashCommand}
            visible={showSlashMenu}
            selectedIndex={slashMenuIndex}
          />
          <Input
            data-testid="chat-input"
            value={input}
            onChange={(e) => {
              setInput(e.target.value)
              setSlashMenuIndex(0)
            }}
            onKeyDown={handleKeyDown}
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
