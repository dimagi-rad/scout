import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom"

import { api } from "@/api/client"
import type { SemanticDataset, SemanticField } from "@/store/datasetSlice"
import { useAppStore } from "@/store/store"
import { DatasetBrowserPage } from "./DatasetBrowserPage"

const WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"

function dataset(overrides: Partial<SemanticDataset>): SemanticDataset {
  return {
    id: "dataset-1",
    name: "raw_visits",
    label: "Raw Visits",
    description: "Visit records",
    source_kind: "physical",
    definition_sql: "",
    schema_name: "tenant_schema",
    table_name: "raw_visits",
    primary_key: "id",
    row_count: 10,
    row_count_verified: false,
    dimensions: [],
    time_dimensions: [],
    measures: [],
    relationships: [],
    metadata: {},
    ...overrides,
  }
}

function field(overrides: Partial<SemanticField>): SemanticField {
  return {
    id: "field-1",
    name: "count",
    member: "raw_visits.count",
    label: "Count",
    description: "",
    type: "measure",
    data_type: "integer",
    measure_type: "count",
    metadata: {},
    ...overrides,
  }
}

const rawVisits = dataset({
  id: "raw-visits",
  name: "raw_visits",
  label: "Raw Visits",
})

const rawPayments = dataset({
  id: "raw-payments",
  name: "raw_payments",
  label: "Raw Payments",
  description: "Payment events",
  table_name: "raw_payments",
})

const visitStats = dataset({
  id: "visit-stats",
  name: "visit_stats",
  label: "Visit Stats",
  description: "Visit rollup",
  source_kind: "custom",
  definition_sql: "select username, count(*) as visit_count\nfrom raw_visits\ngroup by username",
  table_name: "visit_stats",
  metadata: { source_type: "custom" },
})

function catalog() {
  return {
    model: {
      id: "model-1",
      name: "Test Model",
      version: 1,
      status: "active",
      diagnostics: [],
      updated_at: "2026-07-02T12:00:00Z",
    },
    datasets: [rawVisits, rawPayments],
  }
}

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="location">{location.pathname}</div>
}

function renderDatasetPage(initialPath: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route path="/datasets" element={<><LocationProbe /><DatasetBrowserPage /></>} />
        <Route path="/datasets/:datasetName" element={<><LocationProbe /><DatasetBrowserPage /></>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe("DatasetBrowserPage routing", () => {
  beforeEach(() => {
    useAppStore.setState({
      activeDomainId: WORKSPACE_ID,
      datasetCatalog: null,
      datasetStatus: "idle",
      datasetError: null,
      selectedDataset: null,
      selectedDatasetStatus: "idle",
      selectedDatasetError: null,
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("updates the URL when a dataset is selected", async () => {
    const getSpy = vi.spyOn(api, "get").mockImplementation(async (url: string) => {
      if (url === `/api/workspaces/${WORKSPACE_ID}/datasets/`) {
        return catalog() as never
      }
      if (url === `/api/workspaces/${WORKSPACE_ID}/datasets/raw_payments/`) {
        return { model: catalog().model, dataset: rawPayments } as never
      }
      throw new Error(`Unexpected request: ${url}`)
    })

    renderDatasetPage("/datasets")

    await screen.findByTestId("dataset-item-raw_payments")
    await userEvent.click(screen.getByTestId("dataset-item-raw_payments"))

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/datasets/raw_payments")
    })
    expect(getSpy).toHaveBeenCalledWith(`/api/workspaces/${WORKSPACE_ID}/datasets/raw_payments/`)
  })

  it("loads the requested dataset from a direct URL", async () => {
    const detailedPayments = {
      ...rawPayments,
      description: "Fetched payment detail",
    }
    const getSpy = vi.spyOn(api, "get").mockImplementation(async (url: string) => {
      if (url === `/api/workspaces/${WORKSPACE_ID}/datasets/`) {
        return catalog() as never
      }
      if (url === `/api/workspaces/${WORKSPACE_ID}/datasets/raw_payments/`) {
        return { model: catalog().model, dataset: detailedPayments } as never
      }
      throw new Error(`Unexpected request: ${url}`)
    })

    renderDatasetPage("/datasets/raw_payments")

    expect(await screen.findByText("Fetched payment detail")).toBeInTheDocument()
    expect(screen.getByTestId("location")).toHaveTextContent("/datasets/raw_payments")
    expect(getSpy).toHaveBeenCalledWith(`/api/workspaces/${WORKSPACE_ID}/datasets/raw_payments/`)
  })

  it("shows SQL for custom datasets from the catalog response", async () => {
    vi.spyOn(api, "get").mockImplementation(async (url: string) => {
      if (url === `/api/workspaces/${WORKSPACE_ID}/datasets/`) {
        return { ...catalog(), datasets: [visitStats, rawVisits] } as never
      }
      throw new Error(`Unexpected request: ${url}`)
    })

    renderDatasetPage("/datasets")

    expect(await screen.findByText("SQL")).toBeInTheDocument()
    const sqlBlock = screen.getByTestId("custom-dataset-sql")
    expect(sqlBlock).toHaveTextContent("SELECT")
    expect(sqlBlock).toHaveTextContent("count(*) AS visit_count")
    expect(sqlBlock).toHaveTextContent("FROM")
    expect(sqlBlock).toHaveTextContent("raw_visits")
    expect(sqlBlock).toHaveTextContent("GROUP BY")
  })

  it("shows value formats for fields", async () => {
    const formattedVisits = dataset({
      ...rawVisits,
      measures: [
        field({
          id: "total-amount",
          name: "sum_amount",
          member: "raw_visits.sum_amount",
          label: "Total Amount",
          measure_type: "sum",
          metadata: { format: "currency_2", currency: "USD" },
        }),
      ],
      dimensions: [
        field({
          id: "completion-rate",
          name: "completion_rate",
          member: "raw_visits.completion_rate",
          label: "Completion Rate",
          type: "dimension",
          data_type: "numeric",
          measure_type: "",
          metadata: { format: "percent_1" },
        }),
      ],
    })
    vi.spyOn(api, "get").mockImplementation(async (url: string) => {
      if (url === `/api/workspaces/${WORKSPACE_ID}/datasets/`) {
        return { ...catalog(), datasets: [formattedVisits] } as never
      }
      throw new Error(`Unexpected request: ${url}`)
    })

    renderDatasetPage("/datasets")

    expect(await screen.findAllByText("Value format")).toHaveLength(2)
    expect(screen.getByText("currency_2 · USD")).toBeInTheDocument()
    expect(screen.getByText("percent_1")).toBeInTheDocument()
  })
})
