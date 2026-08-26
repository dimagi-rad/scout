import { describe, expect, it } from "vitest"
import {
  fieldKind,
  fieldKindLabel,
  formatDiffKey,
  formatDiffValue,
  groupByDataset,
  pendingObjects,
  type CanvasObjectEntry,
  type CanvasProjection,
} from "./canvasApi"

function makeEntry(overrides: Partial<CanvasObjectEntry> = {}): CanvasObjectEntry {
  return {
    key: "dataset/raw_visits",
    object_type: "dataset",
    object_uuid: "u1",
    change_type: "update",
    name: "raw_visits",
    label: "Visits",
    dataset: "",
    state: "edited",
    summary: "",
    diff: {},
    fields: {},
    base: {},
    ...overrides,
  }
}

function makeProjection(objects: CanvasObjectEntry[]): CanvasProjection {
  return {
    canvas: { id: "c", thread_id: "t", status: "open", committed_at: null, updated_at: "" },
    objects,
    diagnostics: [],
    pending_count: 0,
    can_commit: false,
  }
}

describe("canvasApi helpers", () => {
  it("counts only pending objects", () => {
    const projection = makeProjection([
      makeEntry(),
      makeEntry({ state: "unchanged", object_uuid: "u2" }),
    ])
    expect(pendingObjects(projection)).toHaveLength(1)
    expect(pendingObjects(null)).toEqual([])
  })

  it("formats diff values compactly", () => {
    expect(formatDiffValue("")).toBe("(empty)")
    expect(formatDiffValue(null)).toBe("(empty)")
    expect(formatDiffValue("a\n b")).toBe("a b")
    expect(formatDiffValue("x".repeat(200)).length).toBeLessThanOrEqual(160)
  })

  it("labels value format diffs and classifies field kinds", () => {
    expect(formatDiffKey("format")).toBe("Value format")
    expect(formatDiffKey("currency")).toBe("Currency")
    const measure = makeEntry({
      object_type: "field",
      fields: { field_type: "measure" },
    })
    const dimension = makeEntry({
      object_type: "field",
      base: { field_type: "time_dimension" },
    })
    expect(fieldKind(measure)).toBe("measure")
    expect(fieldKindLabel(dimension)).toBe("time dimension")
  })
})

describe("groupByDataset", () => {
  it("nests fields and relationships under their dataset", () => {
    const groups = groupByDataset(
      makeProjection([
        makeEntry({ state: "unchanged", object_uuid: "d1" }),
        makeEntry({
          object_type: "field",
          object_uuid: "f1",
          change_type: "create",
          name: "total_amount",
          dataset: "raw_visits",
          state: "new",
          fields: { field_type: "measure", measure_type: "sum", expression: "amount" },
        }),
        makeEntry({
          object_type: "relationship",
          object_uuid: "r1",
          change_type: "create",
          name: "visits_to_users",
          dataset: "",
          state: "new",
          fields: { from_dataset: "raw_visits", to_dataset: "raw_users" },
        }),
      ]),
    )

    expect(groups).toHaveLength(1)
    const group = groups[0]
    expect(group.name).toBe("raw_visits")
    expect(group.fields.map((entry) => entry.name)).toEqual(["total_amount"])
    expect(group.relationships.map((entry) => entry.name)).toEqual(["visits_to_users"])
    expect(group.state).toBe("edited")
    expect(group.pendingCount).toBe(2)
    expect(group.summary).toBe("1 measure added · 1 link")
  })

  it("synthesizes a group for edits whose dataset row is not on the canvas", () => {
    const groups = groupByDataset(
      makeProjection([
        makeEntry({
          object_type: "field",
          object_uuid: "f1",
          name: "sum_amount",
          dataset: "raw_payments",
          state: "edited",
          diff: { label: { from: "a", to: "b" } },
        }),
      ]),
    )
    expect(groups).toHaveLength(1)
    expect(groups[0].name).toBe("raw_payments")
    expect(groups[0].dataset).toBeNull()
    expect(groups[0].state).toBe("edited")
    expect(groups[0].summary).toBe("1 field edited")
  })

  it("summarizes measure and dimension diffs separately", () => {
    const groups = groupByDataset(
      makeProjection([
        makeEntry({
          object_type: "field",
          object_uuid: "m1",
          name: "sum_amount",
          dataset: "raw_payments",
          state: "edited",
          diff: { format: { from: "number_2", to: "currency_2" } },
          base: { field_type: "measure" },
        }),
        makeEntry({
          object_type: "field",
          object_uuid: "d1",
          name: "amount",
          dataset: "raw_payments",
          state: "edited",
          diff: { currency: { from: "", to: "USD" } },
          base: { field_type: "dimension" },
        }),
      ]),
    )

    expect(groups[0].summary).toBe("1 measure edited · 1 dimension edited")
  })

  it("marks CTE drafts and escalates conflicts, sorting pending first", () => {
    const groups = groupByDataset(
      makeProjection([
        makeEntry({ state: "unchanged", object_uuid: "d1", name: "raw_users", label: "Users" }),
        makeEntry({
          object_type: "custom_dataset",
          object_uuid: "c1",
          change_type: "create",
          name: "visit_stats",
          label: "Visit Stats",
          state: "new",
          fields: { definition_sql: "select 1", columns: [{ name: "one" }] },
        }),
        makeEntry({ state: "unchanged", object_uuid: "d2", name: "raw_visits" }),
        makeEntry({
          object_type: "field",
          object_uuid: "f1",
          name: "amount",
          dataset: "raw_visits",
          state: "conflict",
        }),
      ]),
    )

    expect(groups.map((group) => group.name)).toEqual(["raw_visits", "visit_stats", "raw_users"])
    expect(groups[0].state).toBe("conflict")
    expect(groups[1].isCte).toBe(true)
    expect(groups[1].summary).toBe("new SQL dataset")
    expect(groups[2].state).toBe("unchanged")
    expect(groups[2].summary).toBe("No pending changes")
  })
})
