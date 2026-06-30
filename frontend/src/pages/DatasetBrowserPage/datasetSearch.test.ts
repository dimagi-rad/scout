import { describe, expect, it } from "vitest"
import type { SemanticDataset } from "@/store/datasetSlice"
import { filterDatasets } from "./datasetSearch"

const makeDataset = (overrides: Partial<SemanticDataset>): SemanticDataset => ({
  id: "dataset-1",
  name: "raw_assessments",
  label: "Raw Assessments",
  description: "Assessment results",
  schema_name: "public",
  table_name: "raw_assessments",
  primary_key: "id",
  row_count: 2,
  row_count_verified: true,
  dimensions: [],
  time_dimensions: [],
  measures: [],
  relationships: [],
  metadata: {},
  ...overrides,
})

const datasets: SemanticDataset[] = [
  makeDataset({
    id: "assessments",
    name: "raw_assessments",
    label: "Raw Assessments",
    measures: [
      {
        id: "score",
        name: "avg_score",
        member: "raw_assessments.avg_score",
        label: "Average Score",
        description: "",
        type: "measure",
        data_type: "number",
        measure_type: "avg",
        metadata: {},
      },
    ],
  }),
  makeDataset({
    id: "payments",
    name: "raw_payments",
    label: "Raw Payments",
    description: "Payment events",
    table_name: "raw_payments",
  }),
]

describe("filterDatasets", () => {
  it("matches dataset titles", () => {
    expect(filterDatasets(datasets, "payments").map((dataset) => dataset.label)).toEqual([
      "Raw Payments",
    ])
  })

  it("matches underscored dataset names with space-separated queries", () => {
    expect(filterDatasets(datasets, "raw assessments").map((dataset) => dataset.name)).toEqual([
      "raw_assessments",
    ])
  })

  it("matches fields inside a dataset", () => {
    expect(filterDatasets(datasets, "average score").map((dataset) => dataset.label)).toEqual([
      "Raw Assessments",
    ])
  })
})
