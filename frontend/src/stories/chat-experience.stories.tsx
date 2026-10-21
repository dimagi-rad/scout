import { useRef, useState } from "react"
import type { UIMessage } from "ai"
import type { Meta, StoryObj } from "@storybook/react-vite"
import { MemoryRouter } from "react-router-dom"

import { ChatMessage } from "@/components/ChatMessage"
import { ChatComposer, QuoteSelectionAction } from "@/components/ChatPanel"
import { ChatThreadHeader } from "@/components/ChatPanel/ChatThreadHeader"
import { TopBarProvider } from "@/components/TopBar"
import { ChatDropZone } from "@/components/ChatPanel/attachments/ChatDropZone"
import { useChatAttachments } from "@/components/ChatPanel/attachments/useChatAttachments"
import { MaterializationProgressBanner } from "@/components/MaterializationStatus/MaterializationProgressBanner"
import type { ActiveJob } from "@/api/jobs"

const activeJob: ActiveJob = {
  thread_job_id: "job-story-active",
  thread_id: "thread-story",
  tool_call_id: "tool-materialize-active",
  job_type: "materialization",
  source_index: 2,
  source_total: 4,
  tenant_name: "Clinic B",
  state: "running",
  created_at: "2026-06-26T14:00:00Z",
  progress: {
    percent: 64,
    rows_loaded: 32000,
    rows_total: 50000,
    unit: "rows",
    message: "Fetching CommCare cases",
    source: "CommCare",
    step: 2,
    total_steps: 3,
  },
}

const initialMessages: UIMessage[] = [
  {
    id: "user-open-cases",
    role: "user",
    parts: [{ type: "text", text: "Which mobile workers have the most open cases?" }],
  } as UIMessage,
  {
    id: "assistant-plan",
    role: "assistant",
    parts: [
      {
        type: "reasoning",
        text: "Need case status, owner assignment, and a grouped count from the semantic model.",
      },
      {
        type: "text",
        text: "I’ll inspect the semantic catalog, then query open cases by owner.",
      },
    ],
  } as unknown as UIMessage,
  {
    id: "assistant-list-tables",
    role: "assistant",
    parts: [
      {
        type: "tool-semantic_catalog",
        toolName: "semantic_catalog",
        toolCallId: "tool-list-1",
        state: "output-available",
        input: {},
        output: JSON.stringify({
          success: true,
          timing_ms: 58,
          data: {
            datasets: [
              { name: "visits", label: "Visits", measures: [{ member: "visits.open_count" }], dimensions: [{ member: "visits.owner_name" }] },
              { name: "workers", label: "Workers", measures: [{ member: "workers.count" }], dimensions: [{ member: "workers.username" }] },
            ],
          },
        }),
      },
    ],
  } as unknown as UIMessage,
  {
    id: "assistant-query",
    role: "assistant",
    parts: [
      {
        type: "tool-semantic_query",
        toolName: "semantic_query",
        toolCallId: "tool-query-1",
        state: "output-available",
        input: {
          measures: ["visits.open_count"],
          dimensions: ["visits.owner_name"],
          limit: 3,
        },
        output: JSON.stringify({
          success: true,
          schema: "workspace_global_operations",
          timing_ms: 184,
          data: {
            columns: ["owner_name", "open_cases", "last_activity"],
            rows: [
              ["Asha Patel", 148, "2026-06-24"],
              ["Jordan Lee", 116, "2026-06-25"],
              ["Mina Okafor", 94, "2026-06-22"],
            ],
            row_count: 3,
            semantic_query: {
              measures: ["visits.open_count"],
              dimensions: ["visits.owner_name"],
              limit: 3,
            },
            members: ["visits.owner_name", "visits.open_count"],
          },
        }),
      },
    ],
  } as unknown as UIMessage,
  {
    id: "assistant-answer",
    role: "assistant",
    parts: [
      {
        type: "text",
        text:
          "Asha Patel has the most open cases, with **148** currently open. Jordan Lee follows with **116**, and Mina Okafor has **94**.",
      },
    ],
  } as unknown as UIMessage,
  {
    id: "user-refresh",
    role: "user",
    parts: [{ type: "text", text: "/refresh-data Pull the latest CommCare cases first." }],
  } as UIMessage,
  {
    id: "assistant-materializing",
    role: "assistant",
    parts: [
      {
        type: "text",
        text: "I’m refreshing the workspace data before answering from the latest records.",
      },
      {
        type: "tool-run_materialization",
        toolName: "run_materialization",
        toolCallId: "tool-materialize-active",
        state: "input-available",
        input: { source: "commcare" },
      },
    ],
  } as unknown as UIMessage,
]

const meta = {
  title: "Chat Experience/Modeled Conversation",
  tags: ["autodocs"],
  parameters: {
    layout: "fullscreen",
  },
} satisfies Meta

export default meta
type Story = StoryObj<typeof meta>

export const TranscriptWithToolsAndPrompt: Story = {
  render: function TranscriptWithToolsAndPromptStory() {
    const [messages, setMessages] = useState<UIMessage[]>(initialMessages)
    const [input, setInput] = useState("")
    const [quotes, setQuotes] = useState<string[]>([])
    const transcriptRef = useRef<HTMLDivElement>(null)

    function appendUserMessage(text: string) {
      setMessages((current) => [
        ...current,
        {
          id: `user-${current.length + 1}`,
          role: "user",
          parts: [{ type: "text", text }],
        } as UIMessage,
        {
          id: `assistant-${current.length + 1}`,
          role: "assistant",
          parts: [
            {
              type: "text",
              text:
                "This story is running with static fixture data. In the app, Scout would stream a tool-backed answer here.",
            },
          ],
        } as UIMessage,
      ])
    }

    return (
      <div className="flex h-[760px] flex-col bg-background">
        <div className="border-b px-5 py-3">
          <div className="text-sm font-medium">Global Operations</div>
          <div className="text-xs text-muted-foreground">Modeled Scout chat transcript</div>
        </div>

        <div ref={transcriptRef} className="flex-1 overflow-y-auto p-5">
          <QuoteSelectionAction
            containerRef={transcriptRef}
            onQuote={(text) =>
              setQuotes((current) => (current.includes(text) ? current : [...current, text]))}
          />
          <div className="mx-auto max-w-4xl space-y-5">
            {messages.map((message, index) => (
              <ChatMessage
                key={message.id}
                message={message}
                isActiveMessage={
                  index === messages.length - 1
                  || message.parts.some((part) => part.type.startsWith("tool-"))
                }
                workspaceId="workspace-story"
                threadId="thread-story"
                activeMaterializationJob={activeJob}
              />
            ))}
          </div>
        </div>

        <MaterializationProgressBanner job={activeJob} workspaceId="workspace-story" />

        <div className="border-t p-4">
          <div className="mx-auto max-w-4xl">
            <ChatComposer
              input={input}
              setInput={setInput}
              onSend={appendUserMessage}
              quotes={quotes}
              onRemoveQuote={(index) => setQuotes((current) => current.filter((_, i) => i !== index))}
              onQuotesSent={() => setQuotes([])}
            />
          </div>
        </div>
      </div>
    )
  },
}

const attachmentMessages: UIMessage[] = [
  {
    id: "user-with-files",
    role: "user",
    parts: [
      { type: "text", text: "Here's the dashboard screenshot and the registry export. Why don't they match?" },
      {
        type: "file",
        mediaType: "image/png",
        filename: "dashboard.png",
        url: "/api/chat/attachments/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/",
      },
      {
        type: "file",
        mediaType: "application/json",
        filename: "registry.json",
        url: "/api/chat/attachments/cccccccc-cccc-cccc-cccc-cccccccccccc/",
      },
    ],
  } as UIMessage,
  {
    id: "assistant-files",
    role: "assistant",
    parts: [{ type: "text", text: "The registry lists 412 sites; the dashboard counts only the 388 active ones." }],
  } as UIMessage,
]

/**
 * Drop files anywhere on the panel, header included, paste a screenshot, or use the
 * paperclip. Uploads go to /api/chat/attachments/, so outside the app they need a mock.
 */
function AttachmentsChat({ className = "h-[760px]" }: { className?: string }) {
  const [messages, setMessages] = useState<UIMessage[]>(attachmentMessages)
  const [input, setInput] = useState("")
  const attachments = useChatAttachments("workspace-story", "thread-story")

  function send(text: string) {
    const files = attachments.take()
    setMessages((current) => [
      ...current,
      {
        id: `user-${current.length + 1}`,
        role: "user",
        parts: [{ type: "text", text }, ...files],
      } as UIMessage,
    ])
  }

  return (
    <ChatDropZone onFiles={attachments.add} className={`flex flex-col bg-background ${className}`}>
      <ChatThreadHeader
        title="Registry vs dashboard"
        panelOpen={false}
        panelMode="files"
        onTitleChange={() => undefined}
        onOpenFiles={() => undefined}
        onOpenCanvas={() => undefined}
      />
      <div className="flex-1 overflow-y-auto p-5">
        <div className="mx-auto max-w-4xl space-y-5">
          {messages.map((message) => (
            <ChatMessage
              key={message.id}
              message={message}
              isActiveMessage={false}
              workspaceId="workspace-story"
              threadId="thread-story"
            />
          ))}
        </div>
      </div>
      <div className="border-t p-4">
        <div className="mx-auto max-w-4xl">
          <ChatComposer input={input} setInput={setInput} onSend={send} attachments={attachments} />
        </div>
      </div>
    </ChatDropZone>
  )
}

export const ChatWithAttachments: Story = {
  render: () => <AttachmentsChat />,
}

/**
 * The chat inside the app's frame: the top bar, a sidebar and an artifact panel (an
 * iframe) all take a dropped file too, as the user drags onto whatever is nearest.
 */
export const ChatWithAttachmentsInAppFrame: Story = {
  render: () => (
    <MemoryRouter>
      <div className="flex h-[760px] bg-background">
        <aside
          className="w-56 shrink-0 border-r p-3 text-sm text-muted-foreground"
          data-testid="sidebar-shell"
        >
          Sidebar
        </aside>
        <div className="flex min-w-0 flex-1 flex-col">
          <TopBarProvider>
            <main className="min-h-0 flex-1">
              <AttachmentsChat className="h-full" />
            </main>
          </TopBarProvider>
        </div>
        <div className="w-80 shrink-0 border-l" data-testid="artifact-modal">
          <iframe
            title="Artifact"
            className="size-full"
            srcDoc="<body style='font:14px sans-serif;padding:16px'>Artifact iframe</body>"
          />
        </div>
      </div>
    </MemoryRouter>
  ),
}
