import type { SemanticDataset } from "@/store/datasetSlice"

const normalize = (value: string): string =>
  value.toLowerCase().replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim()

const datasetSearchValues = (dataset: SemanticDataset): string[] => [
  dataset.name,
  dataset.label,
  dataset.description,
  dataset.schema_name,
  dataset.table_name,
  dataset.primary_key,
  ...dataset.measures.flatMap((field) => [
    field.name,
    field.member,
    field.label,
    field.description,
    field.data_type,
    field.measure_type,
  ]),
  ...dataset.time_dimensions.flatMap((field) => [
    field.name,
    field.member,
    field.label,
    field.description,
    field.data_type,
  ]),
  ...dataset.dimensions.flatMap((field) => [
    field.name,
    field.member,
    field.label,
    field.description,
    field.data_type,
  ]),
  ...dataset.relationships.flatMap((relationship) => [
    relationship.name,
    relationship.from_dataset,
    relationship.to_dataset,
    relationship.relationship_type,
    relationship.join_expression,
  ]),
]

export function filterDatasets(datasets: SemanticDataset[], search: string): SemanticDataset[] {
  const terms = normalize(search).split(" ").filter(Boolean)
  if (terms.length === 0) return datasets

  return datasets.filter((dataset) => {
    const haystack = normalize(datasetSearchValues(dataset).filter(Boolean).join(" "))
    return terms.every((term) => haystack.includes(term))
  })
}
