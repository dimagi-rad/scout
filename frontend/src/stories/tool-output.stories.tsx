import type { Meta, StoryObj } from "@storybook/react-vite"

import { SqlHighlighter } from "@/components/ChatMessage/SqlHighlighter"
import {
  DescribeTableOutput,
  GetMetadataOutput,
  ListTablesOutput,
  QueryToolOutput,
} from "@/components/ChatMessage/ToolOutput"

const querySql = `SELECT owner_name, COUNT(*) AS open_cases
FROM cases
WHERE status = 'open'
GROUP BY owner_name
ORDER BY open_cases DESC
LIMIT 5`

const meta = {
  title: "Chat Primitives/Tool Output",
  tags: ["autodocs"],
  parameters: {
    layout: "centered",
  },
} satisfies Meta

export default meta
type Story = StoryObj<typeof meta>

export const Sql: Story = {
  render: () => (
    <div className="w-[640px] rounded bg-zinc-950 px-4 py-3">
      <pre className="whitespace-pre-wrap">
        <SqlHighlighter sql={querySql} />
      </pre>
    </div>
  ),
}

export const QueryResult: Story = {
  render: () => (
    <div className="w-[720px] rounded-lg border p-4">
      <QueryToolOutput
        output={{
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
            sql_executed: querySql,
            tables_accessed: ["cases", "users"],
          },
        }}
      />
    </div>
  ),
}

export const DescribeTable: Story = {
  render: () => (
    <div className="w-[720px] rounded-lg border p-4">
      <DescribeTableOutput
        output={{
          success: true,
          schema: "workspace_global_operations",
          timing_ms: 92,
          data: {
            name: "cases",
            description: "Current case records from the connected CommCare project.",
            columns: [
              {
                name: "case_id",
                type: "uuid",
                nullable: false,
                description: "Stable CommCare case identifier.",
              },
              {
                name: "owner_name",
                type: "text",
                nullable: true,
                description: "Current owner display name.",
              },
              {
                name: "opened_at",
                type: "timestamp",
                nullable: false,
                description: "Case creation timestamp.",
              },
            ],
          },
        }}
      />
    </div>
  ),
}

export const ListTables: Story = {
  render: () => (
    <div className="w-[560px] rounded-lg border p-4">
      <ListTablesOutput
        output={{
          success: true,
          timing_ms: 58,
          data: {
            note: "Row counts are from the most recent materialization.",
            tables: [
              { name: "cases", materialized_row_count: 12482 },
              { name: "forms", materialized_row_count: 87031 },
              { name: "workers", materialized_row_count: 4218 },
              { name: "groups", materialized_row_count: 114 },
            ],
          },
        }}
      />
    </div>
  ),
}

export const Metadata: Story = {
  render: () => (
    <div className="w-[560px] rounded-lg border p-4">
      <GetMetadataOutput
        output={{
          success: true,
          timing_ms: 71,
          schema: "workspace_global_operations",
          data: {
            table_count: 12,
            relationships: [
              {
                from_table: "cases",
                from_column: "owner_id",
                to_table: "users",
                to_column: "user_id",
              },
            ],
          },
        }}
      />
    </div>
  ),
}

export const ErrorState: Story = {
  render: () => (
    <div className="w-[560px] rounded-lg border p-4">
      <QueryToolOutput
        output={{
          success: false,
          error: {
            code: "permission_denied",
            message: "Query failed",
            detail: "The workspace role does not allow querying this table.",
          },
        }}
      />
    </div>
  ),
}
