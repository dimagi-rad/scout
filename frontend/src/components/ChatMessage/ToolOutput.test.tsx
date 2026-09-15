import { describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {
  GetMetadataOutput,
  QueryToolOutput,
  ListTablesOutput,
  type GetMetadataOutput as GetMetadataOutputType,
  type QueryOutput,
  type ListTablesOutput as ListTablesOutputType,
} from "./ToolOutput"

describe("GetMetadataOutput (arch #246 13#3)", () => {
  it("counts tables from the NAME->detail object map, not Array.isArray", () => {
    // The backend emits `tables` as an object map; the old Array.isArray()
    // check was always false over it, showing "0 tables".
    const output: GetMetadataOutputType = {
      success: true,
      data: {
        schema: "public",
        table_count: 3,
        tables: {
          users: { columns: [] },
          orders: { columns: [] },
          events: { columns: [] },
        },
        relationships: [
          {
            from_table: "orders",
            from_column: "user_id",
            to_table: "users",
            to_column: "id",
          },
        ],
      },
    }
    render(<GetMetadataOutput output={output} />)
    expect(screen.getByText("3 tables")).toBeInTheDocument()
    expect(screen.queryByText("0 tables")).not.toBeInTheDocument()
    expect(screen.getByText("1 relationship")).toBeInTheDocument()
  })

  it("falls back to counting map keys when table_count is absent", () => {
    const output: GetMetadataOutputType = {
      success: true,
      data: { tables: { a: {}, b: {} } },
    }
    render(<GetMetadataOutput output={output} />)
    expect(screen.getByText("2 tables")).toBeInTheDocument()
  })

  it("renders the error-envelope message + code on failure (13#6)", () => {
    const output: GetMetadataOutputType = {
      success: false,
      error: { code: "SCHEMA_BUILD_FAILED", message: "View schema failed to build." },
    }
    render(<GetMetadataOutput output={output} />)
    expect(screen.getByText("View schema failed to build.")).toBeInTheDocument()
    expect(screen.getByText("SCHEMA_BUILD_FAILED")).toBeInTheDocument()
  })
})

describe("QueryToolOutput (arch #246 13#6 / 13#8)", () => {
  it("surfaces error code/message/detail instead of a generic label", () => {
    const output: QueryOutput = {
      success: false,
      error: { code: "QUERY_TIMEOUT", message: "Query exceeded the time limit.", detail: "30s" },
    }
    render(<QueryToolOutput output={output} />)
    expect(screen.getByText("Query exceeded the time limit.")).toBeInTheDocument()
    expect(screen.getByText("QUERY_TIMEOUT")).toBeInTheDocument()
    expect(screen.getByText("30s")).toBeInTheDocument()
  })

  it("JSON-encodes object cells instead of rendering [object Object] (13#8)", () => {
    const output: QueryOutput = {
      success: true,
      data: {
        columns: ["id", "payload"],
        rows: [[1, { k: "v", n: 2 }]],
        row_count: 1,
      },
    }
    render(<QueryToolOutput output={output} />)
    expect(screen.getByText('{"k":"v","n":2}')).toBeInTheDocument()
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument()
  })

  it("preserves apostrophes in string cells (05#2 regression)", () => {
    const output: QueryOutput = {
      success: true,
      data: { columns: ["name"], rows: [["O'Brien"]], row_count: 1 },
    }
    render(<QueryToolOutput output={output} />)
    expect(screen.getByText("O'Brien")).toBeInTheDocument()
  })

  it("renders the executed SQL and tables accessed", async () => {
    const output: QueryOutput = {
      success: true,
      data: {
        columns: ["id"],
        rows: [[1]],
        row_count: 1,
        sql_executed: "select id from users limit 500",
        tables_accessed: ["public.users"],
      },
    }
    render(<QueryToolOutput output={output} />)
    // Results are the default tab; the SQL sits behind its own tab.
    expect(screen.getByText("Tables:")).toBeInTheDocument()
    expect(screen.getByText("public.users")).toBeInTheDocument()
    expect(screen.queryByTestId("query-sql")).not.toBeInTheDocument()

    await userEvent.click(screen.getByTestId("query-tab-sql"))
    expect(screen.getByTestId("query-sql")).toHaveTextContent(/SELECT\s+id\s+FROM\s+users/)
  })

  it("falls back to the input SQL when the query fails", () => {
    const output: QueryOutput = {
      success: false,
      error: { code: "VALIDATION_ERROR", message: "Table not allowed." },
    }
    render(<QueryToolOutput output={output} sql="select * from secrets" />)
    expect(screen.getByText("Table not allowed.")).toBeInTheDocument()
    expect(screen.getByTestId("query-sql")).toHaveTextContent(/SELECT\s+\*\s+FROM\s+secrets/)
  })
})

describe("ListTablesOutput (arch #246 13#6)", () => {
  it("renders the error envelope on failure", () => {
    const output: ListTablesOutputType = {
      success: false,
      error: { code: "CONNECTION_ERROR", message: "Could not reach the database." },
    }
    render(<ListTablesOutput output={output} />)
    expect(screen.getByText("Could not reach the database.")).toBeInTheDocument()
    expect(screen.getByText("CONNECTION_ERROR")).toBeInTheDocument()
  })
})
