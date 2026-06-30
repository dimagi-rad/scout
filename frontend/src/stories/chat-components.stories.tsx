import { useState } from "react"
import type { Meta, StoryObj } from "@storybook/react-vite"

import {
  ChatArtifactButton,
  ChatAvatar,
  ChatReasoningPart,
  ChatTextPart,
  ChatToolCallPart,
} from "@/components/ChatMessage"
import {
  ChatComposer,
  ChatErrorNotice,
  ChatOverloadNotice,
  ChatThinkingIndicator,
} from "@/components/ChatPanel"
import { ChatEmptyPrompt } from "@/components/ChatEmptyState"
import { MaterializationFailure } from "@/components/MaterializationStatus/MaterializationFailure"
import { MaterializationProgressBanner } from "@/components/MaterializationStatus/MaterializationProgressBanner"
import { SlashCommandMenu } from "@/components/ChatPanel/SlashCommandMenu"
import type { ActiveJob, RecentTermination } from "@/api/jobs"

const queryPart = {
  type: "tool-query",
  toolName: "query",
  toolCallId: "tool-query-component",
  state: "output-available",
  input: {},
  output: JSON.stringify({
    success: true,
    schema: "workspace_global_operations",
    timing_ms: 184,
    data: {
      columns: ["owner_name", "open_cases"],
      rows: [
        ["Asha Patel", 148],
        ["Jordan Lee", 116],
      ],
      row_count: 2,
      sql_executed:
        "SELECT owner_name, COUNT(*) AS open_cases\nFROM cases\nWHERE status = 'open'\nGROUP BY owner_name",
      tables_accessed: ["cases"],
    },
  }),
}

const rawToolPart = {
  type: "tool-custom_export",
  toolName: "custom_export",
  toolCallId: "tool-raw-component",
  state: "output-available",
  input: {},
  output: { status: "queued", export_id: "exp_123", rows: 4200 },
}

const errorToolPart = {
  type: "tool-query",
  toolName: "query",
  toolCallId: "tool-error-component",
  state: "output-error",
  input: {},
  errorText: "The database rejected this query because the table does not exist.",
}

const activeMaterializationJob: ActiveJob = {
  thread_job_id: "job-component-active",
  thread_id: "thread-component",
  tool_call_id: "tool-materialize-component",
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

const materializationPart = {
  type: "tool-run_materialization",
  toolName: "run_materialization",
  toolCallId: "tool-materialize-component",
  state: "input-available",
  input: { source: "commcare" },
}

const failedMaterialization: RecentTermination = {
  thread_job_id: "job-component-failed",
  thread_id: "thread-component",
  tool_call_id: "tool-materialize-failed-component",
  state: "failed",
  completed_at: "2026-06-26T14:06:00Z",
  error_summary: "The source returned a 403 while fetching the forms table.",
  retry_available: true,
}

const meta = {
  title: "Chat Primitives/Individual Components",
  tags: ["autodocs"],
  parameters: {
    layout: "centered",
  },
} satisfies Meta

export default meta
type Story = StoryObj<typeof meta>

export const Avatars: Story = {
  render: () => (
    <div className="flex items-center gap-4">
      <ChatAvatar role="assistant" />
      <ChatAvatar role="user" />
    </div>
  ),
}

export const UserTextBubble: Story = {
  render: () => (
    <div className="w-[520px]">
      <ChatTextPart role="user" text="Which mobile workers have the most open cases?" />
    </div>
  ),
}

export const AgentTextBubble: Story = {
  render: () => (
    <div className="w-[520px]">
      <ChatTextPart
        role="assistant"
        text="I found **3 active owners** with open cases. The top owner has 148 open cases."
      />
    </div>
  ),
}

export const ReasoningPanel: Story = {
  render: () => (
    <div className="w-[640px]">
      <ChatReasoningPart
        part={{
          type: "reasoning",
          text: "Need to identify the tables, join case owners to users, then aggregate open case counts by owner.",
        }}
        index={0}
        isLatest
        isActiveMessage
      />
    </div>
  ),
}

export const ToolCallCollapsed: Story = {
  render: () => (
    <div className="w-[680px]">
      <ChatToolCallPart
        part={queryPart}
        index={0}
        isLatest={false}
        isActiveMessage={false}
      />
    </div>
  ),
}

export const ToolCallRichOutput: Story = {
  render: () => (
    <div className="w-[720px]">
      <ChatToolCallPart
        part={queryPart}
        index={0}
        isLatest
        isActiveMessage
      />
    </div>
  ),
}

export const ToolCallRawOutput: Story = {
  render: () => (
    <div className="w-[680px]">
      <ChatToolCallPart
        part={rawToolPart}
        index={0}
        isLatest
        isActiveMessage
      />
    </div>
  ),
}

export const ToolCallError: Story = {
  render: () => (
    <div className="w-[680px]">
      <ChatToolCallPart
        part={errorToolPart}
        index={0}
        isLatest
        isActiveMessage
      />
    </div>
  ),
}

export const ToolCallWithActiveJob: Story = {
  render: () => (
    <div className="w-[680px]">
      <ChatToolCallPart
        part={materializationPart}
        index={0}
        isLatest
        isActiveMessage
        workspaceId="workspace-component"
        threadId="thread-component"
        activeMaterializationJob={activeMaterializationJob}
      />
    </div>
  ),
}

export const ToolCallWithFailure: Story = {
  render: () => (
    <div className="w-[680px]">
      <ChatToolCallPart
        part={{
          ...materializationPart,
          toolCallId: "tool-materialize-failed-component",
        }}
        index={0}
        isLatest={false}
        isActiveMessage={false}
        workspaceId="workspace-component"
        threadId="thread-component"
        recentTermination={failedMaterialization}
      />
    </div>
  ),
}

export const ArtifactButton: Story = {
  render: () => (
    <div className="grid gap-3">
      <ChatArtifactButton artifactId="8fb03f9d-9868-4fb9-a2b8-0ce9f65882ba" />
      <ChatArtifactButton artifactId="8fb03f9d-9868-4fb9-a2b8-0ce9f65882ba" isActive />
    </div>
  ),
}

export const Composer: Story = {
  render: function ComposerStory() {
    const [input, setInput] = useState("How many cases were opened this month?")
    return (
      <div className="w-[720px] rounded-lg border p-4">
        <ChatComposer input={input} setInput={setInput} onSend={() => undefined} />
      </div>
    )
  },
}

export const EmptyChatPrompt: Story = {
  parameters: {
    layout: "fullscreen",
  },
  render: function EmptyChatPromptStory() {
    const [input, setInput] = useState("")
    const lastSyncedAt = new Date(Date.now() - 4 * 60 * 60 * 1000).toISOString()

    return (
      <div className="min-h-[220px] bg-background px-6 py-10">
        <div className="mx-auto max-w-3xl">
          <ChatEmptyPrompt
            input={input}
            setInput={setInput}
            onSend={() => undefined}
            lastSyncedAt={lastSyncedAt}
          />
        </div>
      </div>
    )
  },
}

export const SlashMenu: Story = {
  render: () => (
    <div className="relative w-[520px] rounded-lg border p-4">
      <div className="rounded-md border bg-background px-3 py-2 text-sm text-muted-foreground">
        /r
      </div>
      <SlashCommandMenu
        query="r"
        visible
        selectedIndex={0}
        onSelect={() => undefined}
      />
    </div>
  ),
}

export const ThinkingIndicator: Story = {
  render: () => (
    <div className="w-[240px]">
      <ChatThinkingIndicator />
    </div>
  ),
}

export const ErrorNotice: Story = {
  render: () => (
    <div className="w-[520px]">
      <ChatErrorNotice
        error={new Error("Thread not found")}
        onStartNewThread={() => undefined}
      />
    </div>
  ),
}

export const OverloadNotice: Story = {
  render: () => (
    <div className="w-[520px]">
      <ChatOverloadNotice onRetry={() => undefined} />
    </div>
  ),
}

export const ProgressBanner: Story = {
  parameters: {
    layout: "fullscreen",
  },
  render: () => (
    <div className="mx-auto max-w-3xl py-10">
      <MaterializationProgressBanner
        job={activeMaterializationJob}
        workspaceId="workspace-component"
      />
    </div>
  ),
}

export const FailureCard: Story = {
  render: () => (
    <div className="w-[680px]">
      <MaterializationFailure
        termination={failedMaterialization}
        workspaceId="workspace-component"
        threadId="thread-component"
      />
    </div>
  ),
}
