import { describe, expect, it } from "vitest"
import type { DatasetCatalog, SemanticDataset } from "@/store/datasetSlice"
import {
  applyCanvasChanges,
  collectCanvasDiffs,
  emptyCanvasChanges,
  updateDatasetPatch,
  updateFieldPatch,
  updateRelationshipPatch,
} from "./canvasChanges"

const dataset: SemanticDataset = {
  id: "dataset-1",
  name: "visits",
  label: "Visits",
  description: "Raw visit events",
  schema_name: "public",
  table_name: "visits",
  primary_key: "id",
  row_count: 10,
  row_count_verified: true,
  dimensions: [
    {
      id: "dimension-1",
      name: "status",
      member: "visits.status",
      label: "Status",
      description: "",
      type: "dimension",
      data_type: "string",
      measure_type: "",
      metadata: {},
    },
  ],
  time_dimensions: [],
  measures: [
    {
      id: "measure-1",
      name: "count",
      member: "visits.count",
      label: "Count",
      description: "",
      type: "measure",
      data_type: "number",
      measure_type: "count",
      metadata: {},
    },
  ],
  relationships: [
    {
      id: "relationship-1",
      name: "participant",
      from_dataset: "visits",
      to_dataset: "participants",
      relationship_type: "many_to_one",
      join_expression: "{visits}.participant_id = {participants}.id",
    },
  ],
  metadata: {},
}

const catalog: DatasetCatalog = {
  model: {
    id: "model-1",
    name: "Demo model",
    version: 1,
    status: "active",
    diagnostics: [],
    updated_at: "2026-06-30T12:00:00Z",
  },
  datasets: [dataset],
}

describe("canvasChanges", () => {
  it("removes a dataset edit when the value matches the saved catalog", () => {
    const edited = updateDatasetPatch(emptyCanvasChanges(), dataset.id, "label", "Clinic Visits", dataset.label)
    expect(edited.datasets[dataset.id].dataset?.label).toBe("Clinic Visits")

    const reverted = updateDatasetPatch(edited, dataset.id, "label", dataset.label, dataset.label)
    expect(reverted.datasets).toEqual({})
  })

  it("overlays dataset and field names into member previews", () => {
    const renamedDataset = updateDatasetPatch(
      emptyCanvasChanges(),
      dataset.id,
      "name",
      "clinic_visits",
      dataset.name
    )
    const renamedMeasure = updateFieldPatch(
      renamedDataset,
      dataset.id,
      "measure-1",
      "name",
      "visit_count",
      "count"
    )

    const projected = applyCanvasChanges(dataset, renamedMeasure)
    expect(projected.name).toBe("clinic_visits")
    expect(projected.measures[0].member).toBe("clinic_visits.visit_count")
  })

  it("collects dataset, field, and relationship diffs", () => {
    const withDataset = updateDatasetPatch(
      emptyCanvasChanges(),
      dataset.id,
      "description",
      "Cleaned visit facts",
      dataset.description
    )
    const withField = updateFieldPatch(
      withDataset,
      dataset.id,
      "dimension-1",
      "label",
      "Visit status",
      "Status"
    )
    const withRelationship = updateRelationshipPatch(
      withField,
      dataset.id,
      "relationship-1",
      "name",
      "visit_participant",
      "participant"
    )

    expect(collectCanvasDiffs(catalog, withRelationship)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ targetType: "dataset", property: "description" }),
        expect.objectContaining({ targetType: "field", property: "label" }),
        expect.objectContaining({ targetType: "relationship", property: "name" }),
      ])
    )
  })
})
