import { useState } from "react"
import type { UIMessage } from "ai"
import type { Meta, StoryObj } from "@storybook/react-vite"

import { ChatMessage } from "@/components/ChatMessage"
import { ChatComposer } from "@/components/ChatPanel"
import { MaterializationProgressBanner } from "@/components/MaterializationStatus/MaterializationProgressBanner"
import type { ActiveJob } from "@/api/jobs"

const activeJob: ActiveJob = {
  thread_job_id: "job-story-active",
  thread_id: "thread-story",
  tool_call_id: "tool-materialize-active",
  job_type: "materialization",
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
        text: "Need case status, owner assignment, and a grouped count. Use the current workspace schema.",
      },
      {
        type: "text",
        text: "I’ll inspect the available tables, then query open cases by owner.",
      },
    ],
  } as unknown as UIMessage,
  {
    id: "assistant-list-tables",
    role: "assistant",
    parts: [
      {
        type: "tool-list_tables",
        toolName: "list_tables",
        toolCallId: "tool-list-1",
        state: "output-available",
        input: {},
        output: JSON.stringify({
          success: true,
          timing_ms: 58,
          data: {
            tables: [
              { name: "cases", materialized_row_count: 12482 },
              { name: "forms", materialized_row_count: 87031 },
              { name: "workers", materialized_row_count: 4218 },
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
        type: "tool-query",
        toolName: "query",
        toolCallId: "tool-query-1",
        state: "output-available",
        input: {
          sql: "SELECT owner_name, COUNT(*) AS open_cases FROM cases WHERE status = 'open' GROUP BY owner_name ORDER BY open_cases DESC LIMIT 3",
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
            sql_executed:
              "SELECT owner_name, COUNT(*) AS open_cases\nFROM cases\nWHERE status = 'open'\nGROUP BY owner_name\nORDER BY open_cases DESC\nLIMIT 3",
            tables_accessed: ["cases", "workers"],
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

        <div className="flex-1 overflow-y-auto p-5">
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
            <ChatComposer input={input} setInput={setInput} onSend={appendUserMessage} />
          </div>
        </div>
      </div>
    )
  },
}
