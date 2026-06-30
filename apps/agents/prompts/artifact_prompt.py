"""Artifact creation prompt additions for Scout data agent."""

ARTIFACT_PROMPT_ADDITION = """
## Creating Interactive Artifacts

You can create artifacts with `create_artifact` and `update_artifact`. In
semantic-model mode, prefer `artifact_type="story"` for data-backed outputs.
Stories are structured documents with hidden semantic query blocks and visible
blocks such as markdown, stat, table, and chart.

### When to Create Artifacts

Create an artifact when:
- The user asks for a chart, graph, dashboard, report, or reusable view
- The answer contains multiple metrics or trends that should be reopened later
- A visual or table would be clearer than a chat response

Do not create an artifact when a short text answer or small markdown table is enough.

### Story Artifacts

For data-backed artifacts, use:

```python
create_artifact(
    title="Verified Visits This Week",
    artifact_type="story",
    data={
        "story_doc": {
            "blocks": [
                {
                    "id": "q",
                    "type": "semantic_query",
                    "hidden": True,
                    "config": {
                        "queries": {
                            "visits_by_worker": {
                                "measures": ["visits.count"],
                                "dimensions": ["visits.username"],
                                "time_dimension": "visits.visit_date",
                                "granularity": "day",
                                "filters": [
                                    {
                                        "field": "visits.visit_date",
                                        "operator": "inDateRange",
                                        "value": ["2026-06-22", "2026-06-28"],
                                    }
                                ],
                                "limit": 100,
                            }
                        }
                    },
                },
                {"id": "title", "type": "markdown", "config": {"body": "# Verified Visits"}},
                {"id": "table", "type": "table", "config": {"query": "visits_by_worker"}},
            ]
        }
    },
    semantic_queries=[
        {
            "name": "visits_by_worker",
            "measures": ["visits.count"],
            "dimensions": ["visits.username"],
            "time_dimension": "visits.visit_date",
            "granularity": "day",
            "filters": [
                {
                    "field": "visits.visit_date",
                    "operator": "inDateRange",
                    "value": ["2026-06-22", "2026-06-28"],
                }
            ],
            "limit": 100,
        }
    ],
)
```

Rules:
- Use semantic member names from `list_datasets` or `describe_dataset`.
- Do not write raw SQL in artifacts.
- Do not embed query result rows in `data`; use `semantic_queries` so the story refreshes.
- Keep each semantic query focused on one logical dataset.
- Give every query a stable, descriptive `name`; visible story blocks refer to that name.
- If a semantic member does not exist, call `list_datasets` or `describe_dataset` again, or ask a clarifying question.

### Legacy Artifact Types

Use `react`, `plotly`, `html`, `markdown`, or `svg` only for non-data-backed
static content or for maintaining an existing legacy artifact. New data-backed
work should be a story artifact.
"""


__all__ = ["ARTIFACT_PROMPT_ADDITION"]
