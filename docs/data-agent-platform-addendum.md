# Data Agent Platform — Spec Addendum: Artifacts, Recipes & OAuth

This addendum extends the base design spec with three major additions:

1. **React Artifact System** — Agent can generate rich interactive artifacts (React components, HTML dashboards, charts, markdown reports) rendered in a sandboxed iframe
2. **Recipes** — Save and replay successful analysis workflows as reusable templates
3. **Artifact Sharing** — Share artifacts with other users via URL
4. **Multi-provider OAuth** — Support authentication from external systems

---

## Updated Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                         Frontend Layer                              │
│  ┌──────────────────────┐    ┌──────────────────────────────────┐  │
│  │   Chainlit Chat UI    │    │   Artifact Viewer (iframe)       │  │
│  │   - Message stream    │◄──►│   - Sandboxed React runtime      │  │
│  │   - Project selector  │    │   - Plotly / Recharts / D3       │  │
│  │   - Recipe browser    │    │   - Data injection via postMsg   │  │
│  │   - Tool call display │    │   - Export / share controls      │  │
│  └──────────┬───────────┘    └──────────────┬───────────────────┘  │
└─────────────┼───────────────────────────────┼──────────────────────┘
              │                               │
┌─────────────▼───────────────────────────────▼──────────────────────┐
│                       Django Backend (API)                          │
│  - Project / User / Membership CRUD                                │
│  - Artifact storage & serving                                      │
│  - Recipe CRUD & execution                                         │
│  - Share link generation & access control                          │
│  - OAuth provider management (Google, GitHub, CommCare, custom)    │
│  - Artifact sandbox HTML template serving                          │
└──────────────┬─────────────────────────────────────────────────────┘
               │
┌──────────────▼─────────────────────────────────────────────────────┐
│                    LangGraph Agent Runtime                          │
│  Tools:                                                            │
│  - execute_sql (unchanged from base spec)                          │
│  - create_artifact (NEW - generates React/HTML/Markdown/Plotly)    │
│  - update_artifact (NEW - modify existing artifact in conversation)│
│  - describe_table (unchanged)                                      │
│  - run_recipe (NEW - execute a saved recipe)                       │
└────────────────────────────────────────────────────────────────────┘
```

---

## A1. Artifact System

### Design Philosophy

The agent should be able to produce anything from a simple markdown summary to a fully interactive React dashboard. Rather than building separate tools for tables, charts, maps, and reports, we give the agent a single `create_artifact` tool that accepts code in multiple formats. The frontend renders this in a sandboxed iframe with a pre-configured runtime (React, Recharts, Plotly, D3, Tailwind CSS).

This is the same pattern used by Claude Artifacts and LibreChat — the LLM writes self-contained UI code, and the platform provides the runtime.

### Artifact Types

| Type | Extension | Runtime | Use Case |
|------|-----------|---------|----------|
| `react` | `.jsx` | React 18 + Recharts + Tailwind | Interactive dashboards, data explorers, complex reports |
| `html` | `.html` | Raw HTML + inline JS/CSS | Simple reports, formatted tables, static visualizations |
| `markdown` | `.md` | Markdown renderer | Text reports, summaries, documentation |
| `plotly` | `.json` | Plotly.js | Charts from Plotly JSON specs |
| `svg` | `.svg` | Native SVG | Diagrams, simple graphics |

### Artifact Model

Add to `apps/artifacts/models.py`:

```python
import uuid
import hashlib
from django.db import models
from django.conf import settings


class ArtifactType(models.TextChoices):
    REACT = "react", "React Component"
    HTML = "html", "HTML"
    MARKDOWN = "markdown", "Markdown"
    PLOTLY = "plotly", "Plotly Chart"
    SVG = "svg", "SVG"


class Artifact(models.Model):
    """
    A rendered artifact produced by the agent.

    Artifacts are versioned — each update creates a new version while
    preserving the same artifact ID. The `current_version` field tracks
    the latest version.

    The `code` field contains the source (JSX, HTML, Markdown, etc).
    The `data` field contains any structured data the artifact needs
    (e.g., query results), passed to the React component as props.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="artifacts"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="artifacts"
    )

    # Content
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True)
    artifact_type = models.CharField(max_length=20, choices=ArtifactType.choices)
    code = models.TextField(help_text="Source code: JSX, HTML, Markdown, Plotly JSON, or SVG")
    data = models.JSONField(
        null=True, blank=True,
        help_text="Structured data passed to the artifact (e.g., query results as JSON)"
    )

    # Versioning
    version = models.IntegerField(default=1)
    parent_artifact = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="revisions",
        help_text="Previous version of this artifact"
    )

    # Conversation context
    conversation_id = models.CharField(
        max_length=255, blank=True, db_index=True,
        help_text="Thread ID of the conversation that produced this artifact"
    )
    # Store the SQL queries that generated the data, for reproducibility
    source_queries = models.JSONField(
        default=list, blank=True,
        help_text="SQL queries used to generate the data for this artifact"
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project", "-created_at"]),
            models.Index(fields=["created_by", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.title} (v{self.version})"

    @property
    def content_hash(self) -> str:
        """Hash of code + data for deduplication."""
        content = f"{self.code}:{json.dumps(self.data, sort_keys=True)}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]


class SharedArtifact(models.Model):
    """
    A share link for an artifact. Controls who can view it and for how long.

    Share links can be:
    - Public (anyone with the link)
    - Restricted to project members
    - Restricted to specific users
    - Time-limited (expires_at)
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    artifact = models.ForeignKey(Artifact, on_delete=models.CASCADE, related_name="shares")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    # Access control
    share_token = models.CharField(max_length=64, unique=True, db_index=True)
    access_level = models.CharField(
        max_length=20,
        choices=[
            ("public", "Anyone with link"),
            ("project", "Project members only"),
            ("specific", "Specific users only"),
        ],
        default="project",
    )
    allowed_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True,
        related_name="shared_artifacts_access",
        help_text="Only used when access_level is 'specific'"
    )
    expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Share link expiry. Null = never expires."
    )

    # Tracking
    view_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Share: {self.artifact.title} ({self.access_level})"

    @property
    def share_url(self) -> str:
        return f"/shared/{self.share_token}"

    @property
    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        from django.utils import timezone
        return timezone.now() > self.expires_at
```

### Artifact Sandbox (Frontend Rendering)

Artifacts are rendered in a sandboxed iframe served by Django. The sandbox provides a pre-configured runtime with React, charting libraries, and Tailwind CSS — the agent's code runs inside this environment.

#### `apps/artifacts/views.py` — Sandbox HTML serving

```python
"""
Serves the artifact sandbox HTML.

The sandbox is an HTML page that:
1. Loads React 18, Recharts, Plotly, D3, and Tailwind from CDN
2. Receives artifact code and data via postMessage from the parent frame
3. Transpiles JSX on the fly using Babel standalone
4. Renders the component into a root div
5. Communicates back to the parent (resize, export, errors)

Security:
- iframe sandbox attributes: allow-scripts only (no allow-same-origin)
- No access to parent cookies, localStorage, or DOM
- CSP headers restrict network access to CDN domains only
- Data is injected via postMessage, not URL params
"""
from django.http import HttpResponse, JsonResponse
from django.views import View
from django.utils import timezone
from apps.artifacts.models import Artifact, SharedArtifact


class ArtifactSandboxView(View):
    """Serves the sandbox HTML template for artifact rendering."""

    def get(self, request, artifact_id):
        """Return the sandbox HTML. The actual code/data is injected via postMessage."""
        html = SANDBOX_HTML_TEMPLATE
        response = HttpResponse(html, content_type="text/html")
        # Strict CSP — only allow CDN scripts, no network access from artifact code
        response["Content-Security-Policy"] = (
            "default-src 'none'; "
            "script-src 'unsafe-inline' 'unsafe-eval' "
            "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com; "
            "style-src 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "img-src data: blob:; "
            "font-src https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "connect-src 'none';"  # No fetch/XHR from artifact code
        )
        return response


class ArtifactDataView(View):
    """API endpoint to fetch artifact code and data."""

    def get(self, request, artifact_id):
        try:
            artifact = Artifact.objects.get(id=artifact_id)
        except Artifact.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        # Check access
        if not self._check_access(request, artifact):
            return JsonResponse({"error": "Forbidden"}, status=403)

        return JsonResponse({
            "id": str(artifact.id),
            "title": artifact.title,
            "type": artifact.artifact_type,
            "code": artifact.code,
            "data": artifact.data,
            "version": artifact.version,
        })

    def _check_access(self, request, artifact):
        """Verify the request user has access to this artifact."""
        if not request.user.is_authenticated:
            return False
        return artifact.project.memberships.filter(user=request.user).exists()


class SharedArtifactView(View):
    """Public view for shared artifacts."""

    def get(self, request, share_token):
        try:
            share = SharedArtifact.objects.select_related("artifact").get(
                share_token=share_token
            )
        except SharedArtifact.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        if share.is_expired:
            return JsonResponse({"error": "Share link has expired"}, status=410)

        # Check access level
        if share.access_level == "specific":
            if not request.user.is_authenticated:
                return JsonResponse({"error": "Authentication required"}, status=401)
            if not share.allowed_users.filter(id=request.user.id).exists():
                return JsonResponse({"error": "Forbidden"}, status=403)
        elif share.access_level == "project":
            if not request.user.is_authenticated:
                return JsonResponse({"error": "Authentication required"}, status=401)
            if not share.artifact.project.memberships.filter(user=request.user).exists():
                return JsonResponse({"error": "Forbidden"}, status=403)

        # Track views
        SharedArtifact.objects.filter(id=share.id).update(view_count=models.F("view_count") + 1)

        return JsonResponse({
            "id": str(share.artifact.id),
            "title": share.artifact.title,
            "type": share.artifact.artifact_type,
            "code": share.artifact.code,
            "data": share.artifact.data,
        })


# --- Sandbox HTML Template ---
# This is the HTML page loaded in the iframe that renders artifacts.

SANDBOX_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Artifact Sandbox</title>

    <!-- Tailwind CSS -->
    <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>

    <!-- React 18 -->
    <script crossorigin src="https://cdn.jsdelivr.net/npm/react@18/umd/react.production.min.js"></script>
    <script crossorigin src="https://cdn.jsdelivr.net/npm/react-dom@18/umd/react-dom.production.min.js"></script>

    <!-- Babel for JSX transpilation -->
    <script src="https://cdn.jsdelivr.net/npm/@babel/standalone/babel.min.js"></script>

    <!-- Charting libraries -->
    <script src="https://cdn.jsdelivr.net/npm/recharts@2/umd/Recharts.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2/plotly.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>

    <!-- Lodash for data manipulation -->
    <script src="https://cdn.jsdelivr.net/npm/lodash@4/lodash.min.js"></script>

    <style>
        body { margin: 0; padding: 16px; font-family: system-ui, -apple-system, sans-serif; }
        #root { width: 100%; }
        #error-display { color: #dc2626; padding: 16px; font-family: monospace; white-space: pre-wrap; display: none; }
        #loading { display: flex; align-items: center; justify-content: center; min-height: 200px; color: #6b7280; }
    </style>
</head>
<body>
    <div id="loading">Loading artifact...</div>
    <div id="error-display"></div>
    <div id="root"></div>

    <script>
        // Artifact rendering engine
        (function() {
            const { useState, useEffect, useMemo, useCallback, useRef, createElement } = React;

            // Make libraries available globally for artifact code
            window.React = React;
            window.ReactDOM = ReactDOM;
            window.useState = useState;
            window.useEffect = useEffect;
            window.useMemo = useMemo;
            window.useCallback = useCallback;
            window.useRef = useRef;

            function showError(msg) {
                document.getElementById('loading').style.display = 'none';
                const el = document.getElementById('error-display');
                el.textContent = msg;
                el.style.display = 'block';
                // Notify parent of error
                window.parent.postMessage({ type: 'artifact-error', error: msg }, '*');
            }

            function renderMarkdown(code) {
                // Basic markdown rendering - could use a library for full support
                document.getElementById('loading').style.display = 'none';
                document.getElementById('root').innerHTML = '<div class="prose max-w-none">' + code + '</div>';
            }

            function renderPlotly(jsonStr) {
                document.getElementById('loading').style.display = 'none';
                try {
                    const spec = typeof jsonStr === 'string' ? JSON.parse(jsonStr) : jsonStr;
                    Plotly.newPlot('root', spec.data, spec.layout || {}, { responsive: true });
                } catch (e) {
                    showError('Plotly render error: ' + e.message);
                }
            }

            function renderSVG(code) {
                document.getElementById('loading').style.display = 'none';
                document.getElementById('root').innerHTML = code;
            }

            function renderHTML(code) {
                document.getElementById('loading').style.display = 'none';
                document.getElementById('root').innerHTML = code;
                // Execute any script tags in the HTML
                const scripts = document.getElementById('root').querySelectorAll('script');
                scripts.forEach(script => {
                    const newScript = document.createElement('script');
                    if (script.src) newScript.src = script.src;
                    else newScript.textContent = script.textContent;
                    document.body.appendChild(newScript);
                });
            }

            function renderReact(code, data) {
                document.getElementById('loading').style.display = 'none';
                try {
                    // Make data available as a global for the component
                    window.__ARTIFACT_DATA__ = data || {};

                    // Transpile JSX
                    const transpiledCode = Babel.transform(code, {
                        presets: ['react'],
                        plugins: []
                    }).code;

                    // Create a module-like wrapper that captures the default export
                    const wrappedCode = `
                        (function() {
                            const exports = {};
                            const module = { exports: exports };
                            ${transpiledCode}
                            return module.exports.default || module.exports;
                        })()
                    `;

                    const Component = eval(wrappedCode);

                    if (typeof Component === 'function' || typeof Component === 'object') {
                        const root = ReactDOM.createRoot(document.getElementById('root'));
                        root.render(createElement(Component, { data: window.__ARTIFACT_DATA__ }));
                    } else {
                        showError('Artifact must export a React component as default export');
                    }
                } catch (e) {
                    showError('React render error: ' + e.message + '\\n\\n' + e.stack);
                }
            }

            // Listen for artifact data from parent frame
            window.addEventListener('message', function(event) {
                const { type, artifactType, code, data } = event.data;
                if (type !== 'render-artifact') return;

                try {
                    switch (artifactType) {
                        case 'react': renderReact(code, data); break;
                        case 'html': renderHTML(code); break;
                        case 'markdown': renderMarkdown(code); break;
                        case 'plotly': renderPlotly(code); break;
                        case 'svg': renderSVG(code); break;
                        default: showError('Unknown artifact type: ' + artifactType);
                    }

                    // Notify parent of successful render + content height
                    setTimeout(() => {
                        window.parent.postMessage({
                            type: 'artifact-rendered',
                            height: document.body.scrollHeight
                        }, '*');
                    }, 100);

                } catch (e) {
                    showError('Render error: ' + e.message);
                }
            });

            // Tell parent we're ready
            window.parent.postMessage({ type: 'artifact-sandbox-ready' }, '*');
        })();
    </script>
</body>
</html>"""
```

### Artifact Tool (Agent Side)

Replace the `visualization.py` tool from the base spec with this more general artifact tool.

#### `apps/agents/tools/artifact_tool.py`

```python
"""
Artifact creation tool for the LangGraph agent.

This replaces the simpler visualization tool from the base spec. Instead of
generating static charts, the agent can create rich interactive artifacts
by writing React components, HTML pages, or using Plotly JSON.

The tool:
1. Accepts code (JSX/HTML/Markdown/Plotly JSON/SVG) and optional data
2. Stores the artifact in the database
3. Returns an artifact reference that the frontend renders in a sandbox iframe

The agent is instructed (via system prompt) on what libraries are available
in the sandbox and how to structure React components.

Dependencies:
    No additional deps beyond the base spec.
"""
import uuid
from langchain_core.tools import tool
from typing import Optional, Any


def create_artifact_tools(project, user):
    """
    Factory that creates artifact tools scoped to a project and user.
    Returns a list of tools: [create_artifact, update_artifact]
    """

    @tool
    def create_artifact(
        title: str,
        artifact_type: str,
        code: str,
        description: str = "",
        data: Optional[dict] = None,
        source_queries: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Create a rich interactive artifact for the user.

        Use this tool to create visual outputs: dashboards, charts, reports,
        data tables, or any interactive visualization. The artifact is rendered
        in the user's browser.

        Available artifact types and their environments:
        - "react": A React component (JSX). Default export a function component.
          Available in scope: React, useState, useEffect, useMemo, useCallback, useRef,
          Recharts (BarChart, LineChart, PieChart, AreaChart, etc.), Plotly, d3, lodash.
          Tailwind CSS classes are available for styling.
          The component receives a `data` prop with any structured data you provide.
        - "html": Raw HTML with optional inline JS and CSS. Good for simple reports.
        - "markdown": Markdown text. Best for narrative reports and summaries.
        - "plotly": A Plotly chart specification as JSON string.
        - "svg": An SVG image as a string.

        Args:
            title: Display title for the artifact.
            artifact_type: One of: react, html, markdown, plotly, svg.
            code: The source code for the artifact.
            description: Brief description of what the artifact shows.
            data: Optional structured data (dict/list) passed to the artifact as props.
                  For React artifacts, this is available as `props.data`.
                  Keep this under 500KB — for larger datasets, have the artifact
                  show a summary or paginated view.
            source_queries: Optional list of SQL queries that produced the data,
                           for reproducibility tracking.

        Returns:
            A dict with:
            - "artifact_id": UUID of the created artifact
            - "status": "created"
            - "render_url": URL for the frontend to render this artifact
        """
        from apps.artifacts.models import Artifact

        artifact = Artifact.objects.create(
            project=project,
            created_by=user,
            title=title,
            description=description,
            artifact_type=artifact_type,
            code=code,
            data=data,
            source_queries=source_queries or [],
            conversation_id="",  # Filled in by the handler
        )

        return {
            "artifact_id": str(artifact.id),
            "status": "created",
            "title": title,
            "type": artifact_type,
            "render_url": f"/artifacts/{artifact.id}/sandbox",
        }

    @tool
    def update_artifact(
        artifact_id: str,
        code: str,
        title: Optional[str] = None,
        data: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Update an existing artifact with new code or data.

        Use this when the user asks to modify a previously created artifact —
        for example, changing chart colors, adding a filter, fixing a bug,
        or updating the data.

        Creates a new version of the artifact while preserving the old version.

        Args:
            artifact_id: The UUID of the artifact to update.
            code: The updated source code.
            title: Optional new title (keeps existing if not provided).
            data: Optional updated data (keeps existing if not provided).

        Returns:
            A dict with the new artifact version details.
        """
        from apps.artifacts.models import Artifact

        try:
            original = Artifact.objects.get(id=artifact_id)
        except Artifact.DoesNotExist:
            return {"error": f"Artifact {artifact_id} not found."}

        # Create new version
        new_artifact = Artifact.objects.create(
            project=project,
            created_by=user,
            title=title or original.title,
            description=original.description,
            artifact_type=original.artifact_type,
            code=code,
            data=data if data is not None else original.data,
            source_queries=original.source_queries,
            conversation_id=original.conversation_id,
            version=original.version + 1,
            parent_artifact=original,
        )

        return {
            "artifact_id": str(new_artifact.id),
            "previous_version_id": str(original.id),
            "status": "updated",
            "version": new_artifact.version,
            "title": new_artifact.title,
            "render_url": f"/artifacts/{new_artifact.id}/sandbox",
        }

    return [create_artifact, update_artifact]
```

### Updated System Prompt Addition

Append this to the base system prompt (from Section 5 of the base spec) to instruct the agent on artifact creation:

```python
ARTIFACT_PROMPT_ADDITION = """
## Creating Artifacts

When the user asks for a visualization, report, dashboard, or any rich output,
use the `create_artifact` tool. Choose the appropriate artifact type:

### When to use each type:
- **react**: Interactive dashboards, data explorers, filterable tables, multi-chart
  reports, anything that needs user interaction (dropdowns, tabs, hover effects).
  This is the most capable option — prefer it for complex outputs.
- **plotly**: Quick single charts when you just need a standard chart type.
- **html**: Simple formatted reports, styled tables without interactivity.
- **markdown**: Text-heavy reports, summaries, documentation.
- **svg**: Simple diagrams or static graphics.

### React Artifact Guidelines:
When creating React artifacts, follow these rules:
1. Export a default function component: `export default function MyDashboard({ data }) {...}`
2. The `data` prop contains whatever structured data you pass via the `data` parameter.
3. Available libraries (already loaded, no imports needed):
   - React: useState, useEffect, useMemo, useCallback, useRef
   - Recharts: BarChart, LineChart, PieChart, AreaChart, XAxis, YAxis, CartesianGrid,
     Tooltip, Legend, ResponsiveContainer, Cell, etc.
   - Plotly: window.Plotly for complex charts
   - D3: window.d3 for custom visualizations
   - Lodash: window._ for data manipulation
   - Tailwind CSS: all utility classes available
4. Keep components self-contained — all logic in one file.
5. For large datasets, build pagination or filtering into the component.
6. Use Tailwind for all styling — no separate CSS files.
7. Handle loading and error states gracefully.

### Example React Artifact:
```jsx
export default function SalesReport({ data }) {
  const [selectedRegion, setSelectedRegion] = useState('all');

  const filtered = useMemo(() => {
    if (selectedRegion === 'all') return data.sales;
    return data.sales.filter(s => s.region === selectedRegion);
  }, [data.sales, selectedRegion]);

  const regions = [...new Set(data.sales.map(s => s.region))];

  return (
    <div className="p-4 space-y-6">
      <h1 className="text-2xl font-bold">Sales Report</h1>
      <select
        className="border rounded px-3 py-2"
        value={selectedRegion}
        onChange={e => setSelectedRegion(e.target.value)}
      >
        <option value="all">All Regions</option>
        {regions.map(r => <option key={r} value={r}>{r}</option>)}
      </select>
      <ResponsiveContainer width="100%" height={400}>
        <BarChart data={filtered}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="month" />
          <YAxis />
          <Tooltip />
          <Bar dataKey="revenue" fill="#3b82f6" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
```

### Data Best Practices:
- Run your SQL queries FIRST to get the data, then pass it via the `data` parameter.
- Structure data as the component expects it — don't make the component reshape raw SQL output.
- For multiple charts, organize data like: `{ "chart1": [...], "chart2": [...], "summary": {...} }`
- Include the SQL queries in `source_queries` for reproducibility.
- Keep data under 500KB. For large datasets, aggregate server-side.
"""
```

---

## A2. Recipe System

A recipe captures a successful analysis workflow so it can be replayed with different parameters. Think of it as a saved "macro" that records what the agent did and lets users re-run it.

### How Recipes Work

1. **Capture**: After a successful analysis, the user (or agent) can say "save this as a recipe". The system extracts the sequence of tool calls, their parameters, and identifies which parts are variable (e.g., date ranges, filter values).

2. **Template**: The recipe stores a series of steps, each with a prompt template and expected tool calls. Variables are marked with `{{variable_name}}` syntax.

3. **Execute**: When a user runs a recipe, they provide values for the variables. The system replays the steps, substituting variables, and produces the same artifacts with fresh data.

### Recipe Model

Add to `apps/recipes/models.py`:

```python
import uuid
from django.db import models
from django.conf import settings


class Recipe(models.Model):
    """
    A saved analysis workflow that can be replayed.

    A recipe consists of ordered steps, each of which is a prompt template
    that drives the agent to execute specific tool calls. Variables in the
    templates allow the recipe to be parameterized (e.g., different date
    ranges or filter values).

    Recipes can be created:
    - Manually by defining steps
    - Automatically by extracting steps from a conversation
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="recipes"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="created_recipes"
    )

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    tags = models.JSONField(default=list, blank=True)

    # Variable definitions with metadata
    # Example:
    # [
    #   {"name": "start_date", "type": "date", "label": "Start Date", "default": "2024-01-01"},
    #   {"name": "region", "type": "select", "label": "Region",
    #    "options": ["North", "South", "East", "West"], "default": "North"},
    #   {"name": "min_revenue", "type": "number", "label": "Min Revenue", "default": 1000}
    # ]
    variables = models.JSONField(
        default=list,
        help_text="Variable definitions: name, type (text/number/date/select), label, default, options"
    )

    # Whether this recipe is visible to all project members
    is_shared = models.BooleanField(default=False)

    # Source conversation (if extracted from a conversation)
    source_conversation_id = models.CharField(max_length=255, blank=True)

    # Metadata
    run_count = models.IntegerField(default=0)
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["project", "is_shared"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.project.name})"


class RecipeStep(models.Model):
    """
    A single step in a recipe.

    Each step contains:
    - A prompt template (sent to the agent)
    - Expected tool calls (for validation that the step succeeded)
    - An optional artifact type hint (so the UI can show progress)

    The prompt template can reference variables with {{variable_name}} syntax
    and also reference results from previous steps with {{step_N.result}} syntax.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name="steps")
    order = models.IntegerField()

    name = models.CharField(max_length=255, help_text="Human-readable step name")
    prompt_template = models.TextField(
        help_text="Prompt sent to the agent. Use {{var}} for variables, {{step_N.result}} for prior results."
    )

    # What tool calls this step is expected to make
    # Used for progress tracking and validation, not strict enforcement
    expected_tools = models.JSONField(
        default=list,
        help_text="List of tool names expected: ['execute_sql', 'create_artifact']"
    )

    # If this step produces an artifact, what type?
    produces_artifact = models.BooleanField(default=False)

    # Optional: fixed SQL that should be used (skips agent SQL generation)
    # Useful for recipes where the exact query is known and parameterized
    fixed_sql_template = models.TextField(
        blank=True,
        help_text="If set, this SQL is executed directly instead of having the agent generate it. "
                  "Supports {{variable}} substitution."
    )

    class Meta:
        ordering = ["order"]
        unique_together = ["recipe", "order"]

    def __str__(self):
        return f"{self.recipe.name} - Step {self.order}: {self.name}"

    def render_prompt(self, variables: dict, previous_results: dict = None) -> str:
        """Render the prompt template with variable and step result substitution."""
        import re
        prompt = self.prompt_template

        # Substitute variables
        for key, value in variables.items():
            prompt = prompt.replace(f"{{{{{key}}}}}", str(value))

        # Substitute previous step results
        if previous_results:
            for step_key, result in previous_results.items():
                prompt = prompt.replace(f"{{{{{step_key}.result}}}}", str(result))

        # Check for unresolved variables
        unresolved = re.findall(r"\{\{(\w+)\}\}", prompt)
        if unresolved:
            raise ValueError(f"Unresolved variables in prompt: {unresolved}")

        return prompt


class RecipeRun(models.Model):
    """
    Tracks each execution of a recipe.
    Stores the variable values used and results of each step.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name="runs")
    run_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    # The variable values used for this run
    variable_values = models.JSONField(default=dict)

    # Results per step
    # { "step_1": {"status": "success", "result": "...", "artifact_id": "..."}, ... }
    step_results = models.JSONField(default=dict)

    status = models.CharField(
        max_length=20,
        choices=[
            ("running", "Running"),
            ("completed", "Completed"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        default="running",
    )
    error_message = models.TextField(blank=True)

    # Conversation thread used for this run
    thread_id = models.CharField(max_length=255, blank=True)

    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
```

### Recipe Extraction Tool

Add a tool the agent can use to extract a recipe from the current conversation:

```python
@tool
def save_as_recipe(
    name: str,
    description: str,
    steps: list[dict],
    variables: list[dict],
) -> dict:
    """Save the current analysis workflow as a reusable recipe.

    Call this when the user wants to save a successful analysis for reuse.
    Extract the logical steps from the conversation and identify which
    values should be parameterized.

    Args:
        name: Name for the recipe (e.g., "Monthly Sales Dashboard")
        description: What this recipe produces and when to use it.
        steps: List of step definitions, each with:
            - name: Step name (e.g., "Fetch sales data")
            - prompt_template: The prompt for this step. Use {{variable}} for parameters.
            - expected_tools: List of tool names used (e.g., ["execute_sql"])
            - produces_artifact: Whether this step creates an artifact
            - fixed_sql_template: Optional - exact SQL with {{variables}} if applicable
        variables: List of variable definitions, each with:
            - name: Variable name (used in templates as {{name}})
            - type: One of "text", "number", "date", "select"
            - label: Display label for the UI
            - default: Default value
            - options: For "select" type, list of allowed values

    Returns:
        Recipe ID and confirmation.

    Example:
        save_as_recipe(
            name="Monthly Revenue Report",
            description="Generates a revenue dashboard broken down by region and product",
            variables=[
                {"name": "start_date", "type": "date", "label": "Start Date", "default": "2024-01-01"},
                {"name": "end_date", "type": "date", "label": "End Date", "default": "2024-12-31"},
                {"name": "region", "type": "select", "label": "Region",
                 "options": ["All", "North", "South", "East", "West"], "default": "All"}
            ],
            steps=[
                {
                    "name": "Fetch revenue data",
                    "prompt_template": "Query monthly revenue from {{start_date}} to {{end_date}} for region {{region}}, grouped by month and product category.",
                    "expected_tools": ["execute_sql"],
                    "produces_artifact": False,
                },
                {
                    "name": "Generate dashboard",
                    "prompt_template": "Create a React dashboard showing: 1) Revenue trend line chart by month, 2) Revenue by product category bar chart, 3) Summary statistics table. Use the data from the previous query.",
                    "expected_tools": ["create_artifact"],
                    "produces_artifact": True,
                }
            ]
        )
    """
    from apps.recipes.models import Recipe, RecipeStep

    recipe = Recipe.objects.create(
        project=project,
        created_by=user,
        name=name,
        description=description,
        variables=variables,
        is_shared=False,  # Creator can share later
    )

    for i, step_def in enumerate(steps):
        RecipeStep.objects.create(
            recipe=recipe,
            order=i + 1,
            name=step_def["name"],
            prompt_template=step_def["prompt_template"],
            expected_tools=step_def.get("expected_tools", []),
            produces_artifact=step_def.get("produces_artifact", False),
            fixed_sql_template=step_def.get("fixed_sql_template", ""),
        )

    return {
        "recipe_id": str(recipe.id),
        "name": name,
        "steps_count": len(steps),
        "variables_count": len(variables),
        "status": "saved",
        "message": f"Recipe '{name}' saved with {len(steps)} steps. "
                   f"Users can run it from the recipe browser.",
    }
```

### Recipe Runner Service

```python
"""
apps/recipes/services/runner.py

Executes a recipe by replaying its steps through the agent.
Each step sends a rendered prompt to the agent and collects results.
"""
from apps.recipes.models import Recipe, RecipeRun
from apps.agents.graph.base import build_agent_graph
from langchain_core.messages import HumanMessage
from django.utils import timezone


class RecipeRunner:
    """
    Executes a recipe step by step through the agent.

    Flow:
    1. Validate variable values against recipe variable definitions
    2. Create a RecipeRun record
    3. For each step:
       a. Render the prompt template with variables + prior results
       b. Send to agent (reusing the same thread for context continuity)
       c. Extract results (query output, artifact IDs)
       d. Update step results on RecipeRun
    4. Mark run as completed/failed
    """

    def __init__(self, recipe: Recipe, user, variable_values: dict):
        self.recipe = recipe
        self.user = user
        self.variable_values = variable_values
        self.run = None

    def validate_variables(self):
        """Check that all required variables have values."""
        defined_vars = {v["name"] for v in self.recipe.variables}
        provided_vars = set(self.variable_values.keys())
        missing = defined_vars - provided_vars
        if missing:
            raise ValueError(f"Missing required variables: {missing}")

        # Apply defaults for optional variables not provided
        for var_def in self.recipe.variables:
            if var_def["name"] not in self.variable_values and "default" in var_def:
                self.variable_values[var_def["name"]] = var_def["default"]

    async def execute(self) -> RecipeRun:
        """Run the recipe and return the RecipeRun record."""
        self.validate_variables()

        # Create run record
        self.run = RecipeRun.objects.create(
            recipe=self.recipe,
            run_by=self.user,
            variable_values=self.variable_values,
        )

        # Build agent for this project
        graph = build_agent_graph(self.recipe.project)

        thread_id = f"recipe-run-{self.run.id}"
        self.run.thread_id = thread_id
        self.run.save(update_fields=["thread_id"])

        config = {"configurable": {"thread_id": thread_id}}
        previous_results = {}

        try:
            for step in self.recipe.steps.all():
                # Render prompt
                prompt = step.render_prompt(self.variable_values, previous_results)

                # Execute through agent
                result = await graph.ainvoke(
                    {"messages": [HumanMessage(content=prompt)]},
                    config=config,
                )

                # Extract result summary
                last_msg = result["messages"][-1]
                step_result = {
                    "status": "success",
                    "result_summary": last_msg.content[:1000],  # Truncate for storage
                }

                # Check for artifact creation
                for msg in result["messages"]:
                    if hasattr(msg, "additional_kwargs") and "tool_calls" in msg.additional_kwargs:
                        for tc in msg.additional_kwargs["tool_calls"]:
                            if tc["function"]["name"] == "create_artifact":
                                # Parse the artifact ID from the tool result
                                pass  # Handled by conversation flow

                step_key = f"step_{step.order}"
                previous_results[step_key] = step_result
                self.run.step_results[step_key] = step_result
                self.run.save(update_fields=["step_results"])

            self.run.status = "completed"
            self.run.completed_at = timezone.now()
            self.run.save(update_fields=["status", "completed_at"])

        except Exception as e:
            self.run.status = "failed"
            self.run.error_message = str(e)
            self.run.completed_at = timezone.now()
            self.run.save(update_fields=["status", "error_message", "completed_at"])

        return self.run
```

---

## A3. Multi-Provider OAuth

The platform needs to support OAuth from various identity providers, not just Google/GitHub. This is particularly important for integrating with domain-specific systems (e.g., CommCare, DHIS2, or client-specific identity providers).

### OAuth Architecture

Use `django-allauth` as the OAuth foundation — it supports 80+ providers out of the box and has a clean pattern for adding custom providers.

```python
# config/settings/base.py additions

INSTALLED_APPS = [
    # ...existing apps...
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    # Built-in providers (enable as needed)
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.github",
    "allauth.socialaccount.providers.microsoft",
    "allauth.socialaccount.providers.okta",
    # Custom providers
    "apps.users.providers.commcare",  # Example custom provider
]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# Allauth settings
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_USERNAME_REQUIRED = False
ACCOUNT_AUTHENTICATION_METHOD = "email"
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True

# Per-provider configuration is stored in the database via Django admin
# (SocialApp model), not in settings. This allows runtime configuration
# without redeployment.
```

### Custom OAuth Provider Pattern

For systems not supported by allauth, add a custom provider. Example for CommCare:

```python
"""
apps/users/providers/commcare/provider.py

Custom OAuth2 provider for CommCare HQ.
Follow this pattern for any custom identity provider.
"""
from allauth.socialaccount.providers.base import ProviderAccount
from allauth.socialaccount.providers.oauth2.provider import OAuth2Provider


class CommCareAccount(ProviderAccount):
    def get_avatar_url(self):
        return None

    def to_str(self):
        return self.account.extra_data.get("username", super().to_str())


class CommCareProvider(OAuth2Provider):
    id = "commcare"
    name = "CommCare"
    account_class = CommCareAccount

    def get_default_scope(self):
        return ["read"]

    def extract_uid(self, data):
        return str(data["id"])

    def extract_common_fields(self, data):
        return {
            "email": data.get("email"),
            "username": data.get("username"),
            "first_name": data.get("first_name", ""),
            "last_name": data.get("last_name", ""),
        }


provider_classes = [CommCareProvider]
```

```python
"""
apps/users/providers/commcare/views.py
"""
from allauth.socialaccount.providers.oauth2.views import (
    OAuth2Adapter, OAuth2CallbackView, OAuth2LoginView,
)
from .provider import CommCareProvider


class CommCareOAuth2Adapter(OAuth2Adapter):
    provider_id = CommCareProvider.id

    # These URLs come from the CommCare OAuth documentation
    # Override with settings for different environments
    access_token_url = "https://www.commcarehq.org/oauth/token/"
    authorize_url = "https://www.commcarehq.org/oauth/authorize/"
    profile_url = "https://www.commcarehq.org/api/v0.5/identity/"

    def complete_login(self, request, app, token, **kwargs):
        import requests
        resp = requests.get(
            self.profile_url,
            headers={"Authorization": f"Bearer {token.token}"},
        )
        resp.raise_for_status()
        extra_data = resp.json()
        return self.get_provider().sociallogin_from_response(request, extra_data)


oauth2_login = OAuth2LoginView.adapter_view(CommCareOAuth2Adapter)
oauth2_callback = OAuth2CallbackView.adapter_view(CommCareOAuth2Adapter)
```

### Chainlit Integration with Django Auth

Update the Chainlit auth to work with django-allauth sessions:

```python
"""
chainlit_app/auth.py

Bridge between Chainlit authentication and Django's auth system.
Supports two modes:
1. OAuth via Chainlit's built-in OAuth (delegates to django-allauth)
2. Header-based auth (for reverse proxy setups with SSO)
3. Cookie-based auth (shares Django session cookies)
"""
import chainlit as cl
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
import django
django.setup()

from django.contrib.auth import get_user_model
from apps.projects.models import ProjectMembership

User = get_user_model()


@cl.password_auth_callback
def password_auth(username: str, password: str):
    """
    Basic username/password auth for development.
    In production, use OAuth instead.
    """
    from django.contrib.auth import authenticate
    user = authenticate(username=username, password=password)
    if user:
        return cl.User(
            identifier=user.email,
            metadata={"user_id": str(user.id), "email": user.email, "name": user.get_full_name()}
        )
    return None


@cl.oauth_callback
def oauth_callback(provider_id: str, token: str, raw_user_data: dict, default_user: cl.User):
    """
    Handle OAuth callback from any provider.
    Looks up the Django user associated with this social account.
    """
    from allauth.socialaccount.models import SocialAccount

    # Find the Django user via allauth's social account mapping
    try:
        uid = raw_user_data.get("id") or raw_user_data.get("sub")
        social_account = SocialAccount.objects.get(provider=provider_id, uid=str(uid))
        user = social_account.user
    except SocialAccount.DoesNotExist:
        # Auto-create if SOCIALACCOUNT_AUTO_SIGNUP is True
        email = raw_user_data.get("email")
        if not email:
            return None

        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "first_name": raw_user_data.get("given_name", ""),
                "last_name": raw_user_data.get("family_name", ""),
            }
        )
        SocialAccount.objects.create(
            user=user, provider=provider_id, uid=str(uid),
            extra_data=raw_user_data,
        )

    return cl.User(
        identifier=user.email,
        metadata={
            "user_id": str(user.id),
            "email": user.email,
            "name": user.get_full_name(),
            "provider": provider_id,
        }
    )


@cl.header_auth_callback
def header_auth(headers: dict):
    """
    Header-based auth for reverse proxy setups.
    The proxy (e.g., oauth2-proxy, Authelia) handles authentication
    and passes the user identity via headers.
    """
    email = headers.get("x-forwarded-email") or headers.get("x-auth-request-email")
    if not email:
        return None

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        # Auto-provision user from headers
        user = User.objects.create(
            email=email,
            first_name=headers.get("x-forwarded-preferred-username", ""),
        )

    return cl.User(
        identifier=user.email,
        metadata={"user_id": str(user.id), "email": user.email}
    )
```

---

## A4. Updated Project Structure

```
data-agent-platform/
├── ... (unchanged from base spec)
│
├── apps/
│   ├── projects/                  # (unchanged)
│   ├── agents/
│   │   ├── tools/
│   │   │   ├── sql_tool.py        # Unchanged
│   │   │   ├── artifact_tool.py   # NEW - replaces visualization.py
│   │   │   ├── recipe_tool.py     # NEW - save_as_recipe tool
│   │   │   └── registry.py
│   │   ├── prompts/
│   │   │   ├── base_system.py     # Updated with artifact instructions
│   │   │   ├── artifact_prompt.py # NEW - artifact-specific prompt additions
│   │   │   └── templates.py
│   │   └── ... (rest unchanged)
│   │
│   ├── artifacts/                 # NEW - artifact storage & rendering
│   │   ├── models.py              # Artifact, SharedArtifact
│   │   ├── admin.py
│   │   ├── views.py               # Sandbox HTML, data API, share views
│   │   ├── urls.py
│   │   └── migrations/
│   │
│   ├── recipes/                   # NEW - recipe management
│   │   ├── models.py              # Recipe, RecipeStep, RecipeRun
│   │   ├── admin.py
│   │   ├── services/
│   │   │   └── runner.py          # RecipeRunner
│   │   ├── api/
│   │   │   ├── serializers.py
│   │   │   └── views.py           # Recipe CRUD + run API
│   │   ├── urls.py
│   │   └── migrations/
│   │
│   └── users/
│       ├── providers/             # NEW - custom OAuth providers
│       │   ├── __init__.py
│       │   └── commcare/          # Example custom provider
│       │       ├── provider.py
│       │       ├── views.py
│       │       └── urls.py
│       └── ... (rest unchanged)
│
├── chainlit_app/
│   ├── app.py                     # Updated with artifact iframe rendering
│   ├── auth.py                    # Updated with multi-provider OAuth
│   ├── handlers.py
│   └── artifacts.py               # NEW - artifact display helpers
```

---

## A5. Updated Implementation Phases

Revised to incorporate the new features. Changes from the base spec are marked with ⚡.

### Phase 1: Foundation (Week 1) — unchanged
1. Django project scaffold with settings, User model, Project/Membership models
2. Project admin interface
3. Database credential encryption
4. Data dictionary generator
5. `generate_data_dictionary` management command
6. Tests for data dictionary generation

### Phase 2: Agent Core (Week 2) — mostly unchanged
7. SQL validator with sqlglot
8. SQL tool with connection management, validation, execution
9. Base LangGraph agent graph with system prompt assembly
10. In-memory checkpointer for conversation history
11. ⚡ Artifact model and storage (Artifact, no sharing yet)
12. ⚡ `create_artifact` and `update_artifact` tools
13. Tests: SQL validation, agent end-to-end

### Phase 3: Frontend + Artifacts (Week 3) — major changes
14. Chainlit app with basic password auth
15. Project selection UI
16. Message routing to LangGraph agent
17. ⚡ Artifact sandbox HTML template
18. ⚡ Artifact rendering via iframe in Chainlit (postMessage bridge)
19. ⚡ React artifact support: JSX transpilation, Recharts, Tailwind
20. ⚡ Artifact versioning (update_artifact creates new version)
21. Basic error handling and loading states

### Phase 4: Auth & Sharing (Week 4) — new focus
22. ⚡ django-allauth integration (Google + GitHub providers)
23. ⚡ Chainlit OAuth callback bridge
24. ⚡ SharedArtifact model and share link generation
25. ⚡ Share link access control (public, project, specific users)
26. ⚡ Shared artifact viewer page (standalone, no chat context)
27. PostgreSQL-backed checkpointer
28. Docker Compose for full stack

### Phase 5: Recipes (Week 5) — new phase
29. ⚡ Recipe and RecipeStep models
30. ⚡ `save_as_recipe` tool for the agent
31. ⚡ RecipeRunner service
32. ⚡ Recipe browser UI in Chainlit (list, run with variable form)
33. ⚡ RecipeRun tracking and history
34. ⚡ Recipe sharing between project members

### Phase 6: Polish & Production (Week 6)
35. Connection pooling for project databases
36. Setup script for read-only PostgreSQL roles
37. ⚡ Additional OAuth providers (custom provider pattern)
38. ⚡ Header-based auth for reverse proxy deployments
39. Conversation logging for audit
40. ⚡ Artifact export (download as HTML/PNG/PDF)
41. Rate limiting per user/project
42. Production deployment guide

### Future Enhancements (Backlog)
- Custom React frontend to replace Chainlit (if iframe limitations become blocking)
- Scheduled recipe execution (cron-based reports)
- Recipe marketplace (share recipes across projects)
- Collaborative artifact editing
- Artifact comments and annotations
- Multi-LLM support (switch models per project)
- Row-level security as alternative to schema separation
- Webhook/Slack notifications for recipe completions
- Artifact embedding (embed in external dashboards via iframe)
- Version diff view for artifact history

---

## A6. Updated Dependencies

Add these to `pyproject.toml`:

```toml
dependencies = [
    # ... all existing dependencies from base spec ...

    # OAuth
    "django-allauth>=65.0",

    # Artifact sandbox (no additional server deps - rendering is client-side)
    # But we need these for artifact export/PDF generation:
    "playwright>=1.40",  # For artifact-to-PDF/PNG export (headless browser)
]
```

---

## A7. Key Design Decisions — Addendum

### Why sandboxed iframes for artifacts instead of native Chainlit rendering?

Chainlit's built-in elements (Plotly, Text, Image) are limiting — they don't support interactive React components or custom JS. The iframe sandbox approach gives the agent full creative freedom while maintaining security. The iframe runs with `allow-scripts` only (no `allow-same-origin`), CSP headers prevent network access, and data flows through `postMessage` only. This is the same pattern Claude and ChatGPT use for their artifact/code interpreter features.

### Why not build a custom React frontend from the start?

The MVP benefits from Chainlit's batteries-included chat UI (streaming, auth hooks, file handling, step visualization). The artifact iframe approach keeps most of the value while adding rich rendering. If the iframe integration becomes painful or Chainlit's constraints block important features, Phase 6+ can migrate to a custom frontend — the backend (Django API, LangGraph agent, artifact storage) is frontend-agnostic by design.

### Why django-allauth over building custom OAuth?

allauth handles the OAuth2 dance, token management, account linking, and email verification for 80+ providers. Building this from scratch for each provider is weeks of work. Custom providers (like CommCare) can be added with ~50 lines following allauth's adapter pattern. The provider configuration lives in the database (via Django admin), so new providers can be added without code changes for any standard OAuth2/OIDC system.

### Why separate Recipe and Artifact models instead of combining them?

Recipes are workflows (sequences of prompts and tool calls). Artifacts are outputs (rendered visualizations and reports). A recipe produces artifacts when it runs, but the recipe itself is a template — it can be run many times with different parameters, each time producing different artifacts. Combining them would conflate the "how" (recipe) with the "what" (artifact) and make parameterized re-execution awkward.

### Why not use LangGraph's built-in memory for recipes?

LangGraph's checkpointer handles conversation memory, but recipe execution needs explicit step tracking, variable substitution, and progress reporting. The RecipeRunner orchestrates the agent at a higher level — it sends prompts to the agent for each step and collects results, rather than trying to replay a conversation transcript. This is more robust when the database schema changes or when the agent improves over time.
