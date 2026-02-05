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
// Always use a default export
export default function MyChart({ data }) {
  // data prop contains the JSON data you pass to the artifact

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

### Example React Artifact with Recharts

```jsx
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from "recharts";

export default function RevenueChart({ data }) {
  // data = [{ month: "Jan", revenue: 4000, target: 4500 }, ...]

  return (
    <div className="w-full h-96 p-4">
      <h2 className="text-xl font-semibold mb-4">Monthly Revenue vs Target</h2>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="month" />
          <YAxis />
          <Tooltip formatter={(value) => `$${value.toLocaleString()}`} />
          <Legend />
          <Line
            type="monotone"
            dataKey="revenue"
            stroke="#8884d8"
            strokeWidth={2}
            name="Revenue"
          />
          <Line
            type="monotone"
            dataKey="target"
            stroke="#82ca9d"
            strokeDasharray="5 5"
            name="Target"
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
```

### Data Best Practices

1. **Query data first, then visualize**: Always execute SQL to get the data, then create the artifact with that data.

2. **Pass data via the `data` parameter**: Keep your React code clean by passing data separately:
   ```python
   create_artifact(
       title="Sales by Region",
       artifact_type="react",
       code="...",  # Component code
       data=[{"region": "North", "sales": 1000}, ...],  # Query results
       source_queries=["SELECT region, SUM(amount) as sales FROM orders GROUP BY region"]
   )
   ```

3. **Transform data for visualization**: Reshape SQL results to match what the chart expects:
   - Recharts expects an array of objects with consistent keys
   - Ensure numeric fields are numbers, not strings
   - Format dates appropriately

4. **Include source queries**: Always pass `source_queries` so users can verify the data source.

5. **Handle empty data**: Your React component should handle cases where data is empty or null.

### Updating Artifacts

When a user asks to modify an existing artifact:
1. Use `update_artifact` with the artifact_id from the original creation
2. Provide the complete new code (not a diff)
3. Optionally update the data if the underlying query changed

Example:
```python
update_artifact(
    artifact_id="abc-123-...",
    code="... updated component code ...",
    data=[...],  # Updated data if needed
    title="Updated Title"  # Optional new title
)
```
"""


__all__ = ["ARTIFACT_PROMPT_ADDITION"]
