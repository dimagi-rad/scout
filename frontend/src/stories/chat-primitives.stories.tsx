import { useState } from "react"
import type { UIMessage } from "ai"
import type { Meta, StoryObj } from "@storybook/react-vite"

import { ChatMessage } from "@/components/ChatMessage"
import { ChatComposer } from "@/components/ChatPanel"
import type { ActiveJob, RecentTermination } from "@/api/jobs"

const queryOutput = {
  success: true,
  schema: "workspace_global_operations",
  timing_ms: 184,
  warnings: ["Results were limited to the first 5 rows."],
  data: {
    columns: ["owner_name", "open_cases", "last_activity"],
    rows: [
      ["Asha Patel", 148, "2026-06-24"],
      ["Jordan Lee", 116, "2026-06-25"],
      ["Mina Okafor", 94, "2026-06-22"],
    ],
    row_count: 3,
    truncated: true,
    sql_executed:
      "SELECT owner_name, COUNT(*) AS open_cases\nFROM cases\nWHERE status = 'open'\nGROUP BY owner_name\nORDER BY open_cases DESC\nLIMIT 5",
    tables_accessed: ["cases", "users"],
  },
}

const activeMaterializationJob: ActiveJob = {
  thread_job_id: "job-materialize-1",
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

const failedMaterialization: RecentTermination = {
  thread_job_id: "job-materialize-2",
  thread_id: "thread-story",
  tool_call_id: "tool-materialize-failed",
  state: "failed",
  completed_at: "2026-06-26T14:06:00Z",
  error_summary: "The source returned a 403 while fetching the forms table.",
  retry_available: true,
}

function textMessage(id: string, role: "user" | "assistant", text: string): UIMessage {
  return {
    id,
    role,
    parts: [{ type: "text", text }],
  } as UIMessage
}

function assistantWithTool(id: string, toolName: string, toolCallId: string, output: unknown): UIMessage {
  return {
    id,
    role: "assistant",
    parts: [
      {
        type: "text",
        text: "I checked the available schema and ran a focused query.",
      },
      {
        type: `tool-${toolName}`,
        toolName,
        toolCallId,
        state: "output-available",
        input: {},
        output: JSON.stringify(output),
      },
    ],
  } as unknown as UIMessage
}

function reasoningMessage(): UIMessage {
  return {
    id: "assistant-reasoning",
    role: "assistant",
    parts: [
      {
        type: "reasoning",
        text: "Need to identify the tables, join case owners to users, then aggregate open case counts by owner.",
      },
      {
        type: "text",
        text: "I’ll first inspect the metadata, then query open cases by owner.",
      },
    ],
  } as unknown as UIMessage
}

function materializationMessage(toolCallId: string): UIMessage {
  return {
    id: `assistant-materialization-${toolCallId}`,
    role: "assistant",
    parts: [
      {
        type: "text",
        text: "I’m refreshing the workspace data before answering from the latest records.",
      },
      {
        type: "tool-run_materialization",
        toolName: "run_materialization",
        toolCallId,
        state: "input-available",
        input: { source: "commcare" },
      },
    ],
  } as unknown as UIMessage
}

const meta = {
  title: "Chat Primitives/Messages and Input",
  tags: ["autodocs"],
  parameters: {
    layout: "centered",
  },
} satisfies Meta

export default meta
type Story = StoryObj<typeof meta>

export const UserMessage: Story = {
  render: () => (
    <div className="w-[720px]">
      <ChatMessage
        message={textMessage("user-1", "user", "Which mobile workers have the most open cases?")}
        isActiveMessage={false}
      />
    </div>
  ),
}

export const AgentMessage: Story = {
  render: () => (
    <div className="w-[720px]">
      <ChatMessage
        message={textMessage(
          "assistant-1",
          "assistant",
          "I found **3 active owners** with open cases. The top owner has 148 open cases.",
        )}
        isActiveMessage={false}
      />
    </div>
  ),
}

export const ReasoningPart: Story = {
  render: () => (
    <div className="w-[720px]">
      <ChatMessage message={reasoningMessage()} isActiveMessage={true} />
    </div>
  ),
}

export const QueryToolCall: Story = {
  render: () => (
    <div className="w-[780px]">
      <ChatMessage
        message={assistantWithTool("assistant-query", "query", "tool-query-1", queryOutput)}
        isActiveMessage={true}
      />
    </div>
  ),
}

export const MaterializationToolCall: Story = {
  render: () => (
    <div className="w-[760px]">
      <ChatMessage
        message={materializationMessage("tool-materialize-active")}
        isActiveMessage={true}
        workspaceId="workspace-story"
        threadId="thread-story"
        activeMaterializationJob={activeMaterializationJob}
      />
    </div>
  ),
}

export const FailedToolCall: Story = {
  render: () => (
    <div className="w-[760px]">
      <ChatMessage
        message={materializationMessage("tool-materialize-failed")}
        isActiveMessage={false}
        workspaceId="workspace-story"
        threadId="thread-story"
        recentTerminationsByToolCallId={{
          "tool-materialize-failed": failedMaterialization,
        }}
      />
    </div>
  ),
}

export const ArtifactToolCall: Story = {
  render: () => (
    <div className="w-[720px]">
      <ChatMessage
        message={{
          id: "assistant-artifact",
          role: "assistant",
          parts: [
            {
              type: "tool-create_artifact",
              toolName: "create_artifact",
              toolCallId: "tool-artifact-1",
              state: "output-available",
              input: {},
              output: {
                artifact_id: "8fb03f9d-9868-4fb9-a2b8-0ce9f65882ba",
              },
            },
          ],
        } as unknown as UIMessage}
        isActiveMessage={false}
      />
    </div>
  ),
}

export const PromptInput: Story = {
  render: function PromptInputStory() {
    const [input, setInput] = useState("How many cases were opened this month?")
    return (
      <div className="w-[720px] rounded-lg border p-4">
        <ChatComposer input={input} setInput={setInput} onSend={() => undefined} />
      </div>
    )
  },
}

export const PromptInputWithSlashMenu: Story = {
  render: function PromptInputWithSlashMenuStory() {
    const [input, setInput] = useState("/r")
    return (
      <div className="w-[720px] rounded-lg border p-4 pt-16">
        <ChatComposer input={input} setInput={setInput} onSend={() => undefined} />
      </div>
    )
  },
}

export const StreamingPromptInput: Story = {
  render: function StreamingPromptInputStory() {
    const [input, setInput] = useState("")
    return (
      <div className="w-[720px] rounded-lg border p-4">
        <ChatComposer
          input={input}
          setInput={setInput}
          onSend={() => undefined}
          isStreaming
          onStop={() => undefined}
        />
      </div>
    )
  },
}
