import { describe, expect, it, vi, afterEach, beforeEach } from "vitest"
import { render, screen } from "@testing-library/react"
import { useAppStore } from "@/store/store"
import userEvent from "@testing-library/user-event"
import {
  GetMetadataOutput,
  QueryToolOutput,
  SemanticQueryToolOutput,
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

const data = { columns: ["a", "b"], rows: [["=x", 1], ["y", null]], row_count: 2 }

describe("Download CSV button", () => {
  const setRole = (role: string | null) =>
    useAppStore.setState({
      activeDomainId: "ws1",
      domains: role ? [{ id: "ws1", role }] : [],
    } as never)
  beforeEach(() => setRole("read_write"))
  afterEach(() => vi.restoreAllMocks())

  it.each(["read", null])("is hidden for role %s", (role) => {
    setRole(role)
    render(<QueryToolOutput output={{ success: true, data }} />)
    expect(screen.queryByTestId("query-result-download")).not.toBeInTheDocument()
  })

  it("is shown for manage", () => {
    setRole("manage")
    render(<QueryToolOutput output={{ success: true, data }} />)
    expect(screen.getByTestId("query-result-download")).toBeInTheDocument()
  })

  it("downloads the visible rows as CSV for query results", async () => {
    const urlSpy = vi.fn(() => "blob:x")
    const blobs: Blob[] = []
    URL.createObjectURL = vi.fn((b: Blob | MediaSource) => {
      blobs.push(b as Blob)
      return urlSpy()
    })
    URL.revokeObjectURL = vi.fn()
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {})
    render(<QueryToolOutput output={{ success: true, data }} />)
    const button = screen.getByTestId("query-result-download")
    expect(button).toHaveTextContent("Download CSV")
    await userEvent.click(button)
    expect(click).toHaveBeenCalledTimes(1)
    // Blob.text() strips the UTF-8 BOM, so check the raw bytes for it separately.
    const bytes = new Uint8Array(await blobs[0].arrayBuffer())
    expect([...bytes.slice(0, 3)]).toEqual([0xef, 0xbb, 0xbf])
    expect(await blobs[0].text()).toBe("a,b\r\n'=x,1\r\ny,\r\n")
  })

  it("labels truncated results with the row count", () => {
    render(<QueryToolOutput output={{ success: true, data: { ...data, truncated: true } }} />)
    expect(screen.getByTestId("query-result-download")).toHaveTextContent("Download first 2 rows")
  })

  it("is offered for semantic queries too, and hidden when there are no rows", () => {
    const { unmount } = render(
      <SemanticQueryToolOutput output={{ success: true, data }} />,
    )
    expect(screen.getByTestId("query-result-download")).toBeInTheDocument()
    unmount()
    render(
      <SemanticQueryToolOutput
        output={{ success: true, data: { columns: ["a"], rows: [], row_count: 0 } }}
      />,
    )
    expect(screen.queryByTestId("query-result-download")).not.toBeInTheDocument()
  })
})
