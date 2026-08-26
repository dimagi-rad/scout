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
    semantic_query: {
      measures: ["visits.open_count"],
      dimensions: ["visits.owner_name"],
      time_dimension: "visits.last_activity",
      limit: 5,
    },
    members: ["visits.owner_name", "visits.open_count", "visits.last_activity"],
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
        text: "I checked the semantic catalog and ran a focused semantic query.",
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
        text: "I’ll first inspect the semantic catalog, then query open cases by owner.",
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

function artifactManagerSubagentMessage(state: "loading" | "complete"): UIMessage {
  const parentToolCallId = "tool-artifact-manager-story"
  const basePart = {
    type: "tool-artifact_manager",
    toolName: "artifact_manager",
    toolCallId: parentToolCallId,
    state: state === "loading" ? "input-available" : "output-available",
    input: {
      task: "Create an artifact showing module completion by worker.",
      intent: "create",
    },
  }

  if (state === "loading") {
    return {
      id: "assistant-artifact-manager-loading",
      role: "assistant",
      parts: [basePart],
    } as unknown as UIMessage
  }

  return {
    id: "assistant-artifact-manager-complete",
    role: "assistant",
    parts: [
      {
        ...basePart,
        output: {
          status: "ok",
          artifact_id: "086f1caa-0675-4ff4-b113-21580b5602bf",
          artifact_version: 2,
          touched_blocks: ["title_1", "summary_1", "sq_1", "chart_1", "table_1"],
          diagnostics: [],
          runtime_summary: "3/3 queries ok; chart and table blocks validated.",
          message: "Created 'Module Completion Snapshot' in workspace CHC End-to-End Test.",
        },
      },
      {
        type: "data-subagent-status",
        id: "artifact-manager-status-1",
        data: {
          parentToolCallId,
          subagentName: "artifact_manager",
          phase: "running",
          message: "Planning artifact structure.",
        },
      },
      {
        type: "data-subagent-text",
        id: "artifact-manager-text-1",
        data: {
          parentToolCallId,
          subagentName: "artifact_manager",
          text: "I found the completion metrics and selected title, summary, chart, and table blocks.",
        },
      },
      {
        type: "data-subagent-reasoning",
        id: "artifact-manager-reasoning-1",
        data: {
          parentToolCallId,
          subagentName: "artifact_manager",
          text: "Use one semantic query for the KPI total and one worker-level query for the chart and table.",
        },
      },
      {
        type: "data-subagent-tool-input",
        id: "artifact-manager-overview-input",
        data: {
          parentToolCallId,
          subagentName: "artifact_manager",
          toolCallId: "artifact-manager-overview",
          toolName: "artifact_graph_overview",
          input: {},
        },
      },
      {
        type: "data-subagent-tool-output",
        id: "artifact-manager-overview-output",
        data: {
          parentToolCallId,
          subagentName: "artifact_manager",
          toolCallId: "artifact-manager-overview",
          toolName: "artifact_graph_overview",
          output: {
            blockTypes: ["title", "summary", "semantic_query", "stat", "chart", "table"],
            allowedInputs: ["data", "title", "subtitle", "columns", "encoding"],
          },
        },
      },
      {
        type: "data-subagent-tool-input",
        id: "artifact-manager-queries-input",
        data: {
          parentToolCallId,
          subagentName: "artifact_manager",
          toolCallId: "artifact-manager-queries",
          toolName: "get_artifact_semantic_queries",
          input: { artifact_id: null },
        },
      },
      {
        type: "data-subagent-tool-output",
        id: "artifact-manager-queries-output",
        data: {
          parentToolCallId,
          subagentName: "artifact_manager",
          toolCallId: "artifact-manager-queries",
          toolName: "get_artifact_semantic_queries",
          output: {
            queries: [
              { id: "sq_1.totals", row_count: 1, status: "ok" },
              { id: "sq_1.by_worker", row_count: 3, status: "ok" },
            ],
          },
        },
      },
      {
        type: "data-subagent-tool-input",
        id: "artifact-manager-write-input",
        data: {
          parentToolCallId,
          subagentName: "artifact_manager",
          toolCallId: "artifact-manager-write",
          toolName: "artifact_write",
          input: {
            action: "create",
            title: "Module Completion Snapshot",
            blocks: ["title_1", "summary_1", "sq_1", "stat_1", "chart_1", "table_1"],
          },
        },
      },
      {
        type: "data-subagent-tool-output",
        id: "artifact-manager-write-output",
        data: {
          parentToolCallId,
          subagentName: "artifact_manager",
          toolCallId: "artifact-manager-write",
          toolName: "artifact_write",
          output: {
            status: "ok",
            artifact_id: "086f1caa-0675-4ff4-b113-21580b5602bf",
            diagnostics: [],
          },
        },
      },
      {
        type: "data-subagent-status",
        id: "artifact-manager-status-2",
        data: {
          parentToolCallId,
          subagentName: "artifact_manager",
          phase: "complete",
          message: "Artifact validated successfully.",
        },
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
        message={assistantWithTool("assistant-query", "semantic_query", "tool-query-1", queryOutput)}
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

export const ArtifactManagerSubagent: Story = {
  render: () => (
    <div className="w-[780px]">
      <ChatMessage
        message={artifactManagerSubagentMessage("complete")}
        isActiveMessage={false}
        workspaceId="workspace-story"
        threadId="thread-story"
      />
    </div>
  ),
}

export const ArtifactManagerSubagentLoading: Story = {
  render: () => (
    <div className="w-[780px]">
      <ChatMessage
        message={artifactManagerSubagentMessage("loading")}
        isActiveMessage
        workspaceId="workspace-story"
        threadId="thread-story"
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
