"""
Artifact views for Scout data agent platform.

Provides views for rendering artifacts in a sandboxed iframe,
fetching artifact data via API, and serving shared artifacts.
"""
from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.projects.models import ProjectMembership

from .models import AccessLevel, Artifact, SharedArtifact


# Content Security Policy for the sandbox iframe
SANDBOX_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com; "
    "style-src 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src data: blob:; "
    "font-src https://cdn.jsdelivr.net; "
    "connect-src 'none';"
)


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

    <!-- Babel for JSX transformation -->
    <script src="https://cdn.jsdelivr.net/npm/@babel/standalone@7/babel.min.js"></script>

    <!-- Recharts for React charts -->
    <script src="https://cdn.jsdelivr.net/npm/recharts@2/umd/Recharts.min.js"></script>

    <!-- Plotly for advanced charts -->
    <script src="https://cdn.jsdelivr.net/npm/plotly.js-dist@2/plotly.min.js"></script>

    <!-- D3 for custom visualizations -->
    <script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>

    <!-- Lodash for data manipulation -->
    <script src="https://cdn.jsdelivr.net/npm/lodash@4/lodash.min.js"></script>

    <!-- Marked for Markdown rendering -->
    <script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>

    <style>
        * {
            box-sizing: border-box;
        }
        html, body {
            margin: 0;
            padding: 0;
            width: 100%;
            height: 100%;
            overflow: hidden;
        }
        #root {
            width: 100%;
            height: 100%;
            display: flex;
            flex-direction: column;
        }
        #artifact-container {
            flex: 1;
            width: 100%;
            overflow: auto;
            padding: 16px;
        }
        .loading-state {
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100%;
            color: #6b7280;
            font-family: system-ui, -apple-system, sans-serif;
        }
        .loading-spinner {
            width: 32px;
            height: 32px;
            border: 3px solid #e5e7eb;
            border-top-color: #3b82f6;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin-right: 12px;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        .error-state {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100%;
            padding: 24px;
            text-align: center;
            font-family: system-ui, -apple-system, sans-serif;
        }
        .error-icon {
            width: 48px;
            height: 48px;
            color: #ef4444;
            margin-bottom: 16px;
        }
        .error-title {
            font-size: 18px;
            font-weight: 600;
            color: #1f2937;
            margin-bottom: 8px;
        }
        .error-message {
            font-size: 14px;
            color: #6b7280;
            max-width: 400px;
            word-break: break-word;
        }
        .error-details {
            margin-top: 16px;
            padding: 12px;
            background: #fef2f2;
            border: 1px solid #fecaca;
            border-radius: 8px;
            font-family: monospace;
            font-size: 12px;
            color: #991b1b;
            max-width: 100%;
            overflow-x: auto;
            white-space: pre-wrap;
            text-align: left;
        }
    </style>
</head>
<body>
    <div id="root">
        <div id="artifact-container">
            <div class="loading-state" id="loading">
                <div class="loading-spinner"></div>
                <span>Waiting for artifact...</span>
            </div>
        </div>
    </div>

    <script>
        // Artifact rendering system
        const ArtifactRenderer = {
            container: null,
            currentArtifact: null,

            init() {
                this.container = document.getElementById('artifact-container');
                window.addEventListener('message', this.handleMessage.bind(this));
                // Signal that we're ready to receive artifacts
                window.parent.postMessage({ type: 'sandbox-ready' }, '*');
            },

            handleMessage(event) {
                const { type, artifact } = event.data || {};
                if (type === 'render-artifact' && artifact) {
                    this.render(artifact);
                }
            },

            render(artifact) {
                this.currentArtifact = artifact;
                this.hideLoading();

                try {
                    switch (artifact.type) {
                        case 'react':
                            this.renderReact(artifact);
                            break;
                        case 'html':
                            this.renderHTML(artifact);
                            break;
                        case 'markdown':
                            this.renderMarkdown(artifact);
                            break;
                        case 'plotly':
                            this.renderPlotly(artifact);
                            break;
                        case 'svg':
                            this.renderSVG(artifact);
                            break;
                        default:
                            this.showError('Unknown artifact type', `Type "${artifact.type}" is not supported.`);
                    }
                } catch (error) {
                    this.showError('Render Error', error.message, error.stack);
                }
            },

            renderReact(artifact) {
                const { code, data } = artifact;

                // Create a fresh container for React
                this.container.innerHTML = '<div id="react-root"></div>';
                const reactRoot = document.getElementById('react-root');

                try {
                    // Transform JSX to JavaScript using Babel
                    const transformed = Babel.transform(code, {
                        presets: ['react'],
                        filename: 'artifact.jsx'
                    }).code;

                    // Create a function that returns the component
                    // Provide common libraries and the data prop
                    const componentFactory = new Function(
                        'React',
                        'ReactDOM',
                        'Recharts',
                        'd3',
                        '_',
                        'data',
                        `
                        const { useState, useEffect, useRef, useMemo, useCallback, memo, Fragment } = React;
                        const {
                            LineChart, Line, AreaChart, Area, BarChart, Bar,
                            PieChart, Pie, Cell, ScatterChart, Scatter,
                            XAxis, YAxis, CartesianGrid, Tooltip, Legend,
                            ResponsiveContainer, ComposedChart, RadarChart, Radar,
                            PolarGrid, PolarAngleAxis, PolarRadiusAxis,
                            Treemap, Sankey, FunnelChart, Funnel
                        } = Recharts;

                        ${transformed}

                        // Try to find the default export or a component named App/Component
                        const Component = typeof exports !== 'undefined' ? exports.default :
                                         typeof App !== 'undefined' ? App :
                                         typeof Component !== 'undefined' ? Component :
                                         typeof Chart !== 'undefined' ? Chart :
                                         typeof Visualization !== 'undefined' ? Visualization : null;
                        return Component;
                        `
                    );

                    const Component = componentFactory(
                        React,
                        ReactDOM,
                        Recharts,
                        d3,
                        _,
                        data || {}
                    );

                    if (Component) {
                        const root = ReactDOM.createRoot(reactRoot);
                        root.render(React.createElement(Component, { data: data || {} }));
                    } else {
                        this.showError('Component Not Found', 'Could not find a valid React component to render. Make sure your code exports a component or defines App, Component, Chart, or Visualization.');
                    }
                } catch (error) {
                    this.showError('React Render Error', error.message, error.stack);
                }
            },

            renderHTML(artifact) {
                const { code, data } = artifact;

                // If there's data, we might need to interpolate it
                let html = code;
                if (data) {
                    // Simple template interpolation for {{variable}} syntax
                    html = code.replace(/\\{\\{\\s*(\\w+)\\s*\\}\\}/g, (match, key) => {
                        return data[key] !== undefined ? String(data[key]) : match;
                    });
                }

                this.container.innerHTML = html;

                // Execute any scripts in the HTML
                const scripts = this.container.querySelectorAll('script');
                scripts.forEach(script => {
                    const newScript = document.createElement('script');
                    if (script.src) {
                        newScript.src = script.src;
                    } else {
                        newScript.textContent = script.textContent;
                    }
                    script.parentNode.replaceChild(newScript, script);
                });
            },

            renderMarkdown(artifact) {
                const { code } = artifact;

                try {
                    // Configure marked for security
                    marked.setOptions({
                        breaks: true,
                        gfm: true,
                        headerIds: false,
                        mangle: false
                    });

                    const html = marked.parse(code);
                    this.container.innerHTML = `
                        <article class="prose prose-slate max-w-none">
                            ${html}
                        </article>
                    `;
                } catch (error) {
                    this.showError('Markdown Render Error', error.message);
                }
            },

            renderPlotly(artifact) {
                const { code, data } = artifact;

                this.container.innerHTML = '<div id="plotly-root" style="width: 100%; height: 100%;"></div>';
                const plotlyRoot = document.getElementById('plotly-root');

                try {
                    // Parse the Plotly configuration
                    let config;
                    if (typeof code === 'string') {
                        // If code is a string, try to parse it as JSON first
                        try {
                            config = JSON.parse(code);
                        } catch {
                            // If not JSON, evaluate it as JavaScript that returns a config
                            const configFactory = new Function('data', 'Plotly', 'd3', '_', `return ${code}`);
                            config = configFactory(data || {}, Plotly, d3, _);
                        }
                    } else {
                        config = code;
                    }

                    // Merge with any provided data
                    if (data && config.data) {
                        config.data = config.data.map((trace, i) => ({
                            ...trace,
                            ...(data.traces ? data.traces[i] : {})
                        }));
                    }

                    const layout = {
                        autosize: true,
                        margin: { t: 40, r: 20, b: 40, l: 50 },
                        ...config.layout
                    };

                    const plotConfig = {
                        responsive: true,
                        displayModeBar: true,
                        ...config.config
                    };

                    Plotly.newPlot(plotlyRoot, config.data || [], layout, plotConfig);
                } catch (error) {
                    this.showError('Plotly Render Error', error.message, error.stack);
                }
            },

            renderSVG(artifact) {
                const { code, data } = artifact;

                try {
                    // If code contains JavaScript (for D3), execute it
                    if (code.includes('d3.') || code.includes('function')) {
                        this.container.innerHTML = '<svg id="svg-root" width="100%" height="100%"></svg>';
                        const svgRoot = d3.select('#svg-root');

                        const renderFn = new Function('svg', 'd3', 'data', '_', code);
                        renderFn(svgRoot, d3, data || {}, _);
                    } else {
                        // Otherwise, treat it as raw SVG markup
                        this.container.innerHTML = code;
                    }
                } catch (error) {
                    this.showError('SVG Render Error', error.message, error.stack);
                }
            },

            hideLoading() {
                const loading = document.getElementById('loading');
                if (loading) {
                    loading.style.display = 'none';
                }
            },

            showError(title, message, details = null) {
                this.container.innerHTML = `
                    <div class="error-state">
                        <svg class="error-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                                  d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>
                        </svg>
                        <div class="error-title">${this.escapeHtml(title)}</div>
                        <div class="error-message">${this.escapeHtml(message)}</div>
                        ${details ? `<div class="error-details">${this.escapeHtml(details)}</div>` : ''}
                    </div>
                `;

                // Notify parent of error
                window.parent.postMessage({
                    type: 'artifact-error',
                    error: { title, message, details }
                }, '*');
            },

            escapeHtml(text) {
                const div = document.createElement('div');
                div.textContent = text;
                return div.innerHTML;
            }
        };

        // Initialize when DOM is ready
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => ArtifactRenderer.init());
        } else {
            ArtifactRenderer.init();
        }
    </script>
</body>
</html>"""


class ArtifactSandboxView(View):
    """
    Serves the sandbox HTML template for rendering artifacts in an iframe.

    The sandbox page loads React, Recharts, Plotly, D3, and other libraries
    from CDN and listens for postMessage events to render artifacts securely.
    """

    def get(self, request: HttpRequest, artifact_id: str) -> HttpResponse:
        """Return the sandbox HTML with strict CSP headers."""
        # Verify artifact exists (but don't require auth for sandbox page itself)
        # The actual artifact data is fetched separately via ArtifactDataView
        get_object_or_404(Artifact, pk=artifact_id)

        response = HttpResponse(SANDBOX_HTML_TEMPLATE, content_type="text/html")
        response["Content-Security-Policy"] = SANDBOX_CSP
        response["X-Content-Type-Options"] = "nosniff"
        response["X-Frame-Options"] = "SAMEORIGIN"
        return response


@method_decorator(csrf_exempt, name="dispatch")
class ArtifactDataView(View):
    """
    API endpoint to fetch artifact code and data.

    Returns JSON with artifact details for rendering in the sandbox.
    Requires project membership for access.
    """

    def get(self, request: HttpRequest, artifact_id: str) -> JsonResponse:
        """Fetch artifact data for rendering."""
        artifact = get_object_or_404(Artifact, pk=artifact_id)

        # Check access via project membership
        if not request.user.is_authenticated:
            return JsonResponse(
                {"error": "Authentication required"},
                status=401
            )

        has_access = ProjectMembership.objects.filter(
            user=request.user,
            project=artifact.project
        ).exists()

        if not has_access and not request.user.is_superuser:
            return JsonResponse(
                {"error": "Access denied. You are not a member of this project."},
                status=403
            )

        return JsonResponse(self._serialize_artifact(artifact))

    def _serialize_artifact(self, artifact: Artifact) -> dict[str, Any]:
        """Serialize artifact for JSON response."""
        return {
            "id": str(artifact.id),
            "title": artifact.title,
            "type": artifact.artifact_type,
            "code": artifact.code,
            "data": artifact.data,
            "version": artifact.version,
        }


@method_decorator(csrf_exempt, name="dispatch")
class SharedArtifactView(View):
    """
    Public view for accessing shared artifacts via token.

    Checks access level, expiration, and allowed users before
    returning artifact data. Increments view count on access.
    """

    def get(self, request: HttpRequest, share_token: str) -> JsonResponse:
        """Fetch shared artifact data."""
        share = get_object_or_404(
            SharedArtifact.objects.select_related("artifact", "artifact__project"),
            share_token=share_token
        )

        # Check if share is expired
        if share.is_expired:
            return JsonResponse(
                {"error": "This share link has expired."},
                status=403
            )

        # Check access based on access level
        if share.access_level == AccessLevel.PUBLIC:
            # Public links are accessible to anyone
            pass
        elif share.access_level == AccessLevel.PROJECT:
            # Project-level access requires authentication and project membership
            if not request.user.is_authenticated:
                return JsonResponse(
                    {"error": "Authentication required to access this artifact."},
                    status=401
                )
            if not share.artifact.project.memberships.filter(user=request.user).exists():
                return JsonResponse(
                    {"error": "You must be a project member to access this artifact."},
                    status=403
                )
        elif share.access_level == AccessLevel.SPECIFIC:
            # Specific user access requires authentication and being in allowed_users
            if not request.user.is_authenticated:
                return JsonResponse(
                    {"error": "Authentication required to access this artifact."},
                    status=401
                )
            if not share.allowed_users.filter(pk=request.user.pk).exists():
                return JsonResponse(
                    {"error": "You do not have permission to access this artifact."},
                    status=403
                )

        # Record the view
        share.increment_view_count()

        # Return artifact data
        artifact = share.artifact
        return JsonResponse({
            "id": str(artifact.id),
            "title": artifact.title,
            "type": artifact.artifact_type,
            "code": artifact.code,
            "data": artifact.data,
            "version": artifact.version,
            "access_level": share.access_level,
        })
