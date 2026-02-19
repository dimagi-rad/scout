"""
Artifact creation prompt additions for Scout data agent.

This module provides prompt text that instructs the agent on how and when
to create interactive artifacts. The prompt covers:
- When to use each artifact type
- React component guidelines and available libraries
- Example code patterns
- Data handling best practices
"""

ARTIFACT_PROMPT_ADDITION = """
## Creating Interactive Artifacts

You have the ability to create interactive visualizations and content using the `create_artifact` and `update_artifact` tools. Use these when the user's question would benefit from a visual representation rather than just text and tables.

### When to Create Artifacts

Create an artifact when:
- The user asks for a chart, graph, or visualization
- Data would be clearer as a visual (trends, comparisons, distributions)
- The user requests a dashboard or interactive view
- Complex data relationships need to be shown
- The user explicitly asks for a "visualization" or "chart"

Do NOT create an artifact when:
- A simple markdown table suffices
- The user just wants raw numbers
- The data set is very small (< 5 rows) and simple
- The user explicitly asks for text/table format

### Artifact Types

Choose the appropriate artifact type based on the use case:

**react** (Recommended for most visualizations)
- Interactive dashboards and complex visualizations
- Charts with user interactions (hover, click, zoom)
- Multi-chart layouts and data grids
- Use when you need maximum flexibility

**plotly**
- Statistical charts and scientific visualizations
- 3D plots, contour plots, heatmaps
- When you need Plotly-specific chart types
- Pass the Plotly figure specification as JSON in the code field

**html**
- Simple formatted tables with styling
- Static content with custom CSS
- Embeddable widgets
- When React overhead isn't needed

**markdown**
- Documentation and reports
- Formatted text with code blocks
- Content that will be exported or shared as text

**svg**
- Custom diagrams and flowcharts
- Icons and simple graphics
- When you need precise vector control

### React Artifact Guidelines

For React artifacts, follow these patterns:

**Available Libraries (pre-loaded, no imports needed from CDN):**
- `recharts` - For charts (LineChart, BarChart, PieChart, AreaChart, etc.)
- `react` - Core React (useState, useEffect, useMemo, etc.)
- `lucide-react` - Icons

**Component Structure:**
```jsx
export default function MyChart({ data }) {
  // data keys match the "name" fields from source_queries
  // e.g. data.monthly_revenue is an array of row objects
  const rows = data.monthly_revenue || [];

  return (
    <div className="p-4">
      {/* Your visualization */}
    </div>
  );
}
```

**Styling:**
- Tailwind CSS classes are available (p-4, flex, grid, text-lg, etc.)
- Use inline styles for dynamic values
- Keep visualizations responsive with relative widths

### Live Data via source_queries (IMPORTANT)

Artifacts fetch live data at render time. You MUST provide `source_queries` with
the SQL queries that produce the data your component needs. Do NOT embed query
result rows in the `data` parameter -- the system executes the queries against
the project database every time the artifact is viewed, so data is always fresh.

Each source query is a dict with "name" and "sql" keys:
```python
source_queries=[
    {"name": "monthly_revenue", "sql": "SELECT date_trunc('month', ordered_at) AS month, SUM(total_price) AS revenue FROM orders GROUP BY 1 ORDER BY 1"},
    {"name": "top_products", "sql": "SELECT p.name, SUM(oi.quantity) AS units_sold FROM order_items oi JOIN products p ON oi.product_id = p.id GROUP BY 1 ORDER BY 2 DESC LIMIT 10"},
]
```

The component receives `data.monthly_revenue` (array of row objects with column-name
keys) and `data.top_products`. If a query returns exactly one row, it is provided as
a single object instead of an array.

### Example: React Artifact with Live Queries

```python
create_artifact(
    title="Monthly Revenue vs Target",
    artifact_type="react",
    code='''
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from "recharts";

export default function RevenueChart({ data }) {
  const rows = data.monthly_revenue || [];

  return (
    <div className="w-full h-96 p-4">
      <h2 className="text-xl font-semibold mb-4">Monthly Revenue</h2>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="month" />
          <YAxis />
          <Tooltip formatter={(value) => `$${Number(value).toLocaleString()}`} />
          <Legend />
          <Line type="monotone" dataKey="revenue" stroke="#8884d8" strokeWidth={2} name="Revenue" />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
    ''',
    source_queries=[
        {"name": "monthly_revenue", "sql": "SELECT date_trunc('month', ordered_at)::date AS month, SUM(total_price) AS revenue FROM orders GROUP BY 1 ORDER BY 1"}
    ]
)
```

### Data Best Practices

1. **Always provide source_queries**: For any data-driven artifact, pass the SQL
   queries that produce the data. This is how the artifact gets live data.

2. **Name queries to match component expectations**: The query "name" becomes the
   key on the data prop. Pick clear, descriptive names.

3. **Handle missing/empty data gracefully**: Always default with `|| []` or `|| {}`
   so the component renders a meaningful empty state rather than crashing.

4. **Use the `data` parameter only for static config**: Things like color palettes,
   thresholds, or labels that are not query results. Query results come from
   source_queries automatically.

5. **Keep queries focused**: One query per logical dataset. A dashboard with KPIs,
   a trend chart, and a top-N list should have three separate named queries.

### Updating Artifacts

When a user asks to modify an existing artifact:
1. Use `update_artifact` with the artifact_id from the original creation
2. Provide the complete new code (not a diff)
3. Optionally update source_queries if the underlying queries changed

Example:
```python
update_artifact(
    artifact_id="abc-123-...",
    code="... updated component code ...",
    source_queries=[{"name": "revenue", "sql": "SELECT ..."}],
    title="Updated Title"
)
```
"""


__all__ = ["ARTIFACT_PROMPT_ADDITION"]
