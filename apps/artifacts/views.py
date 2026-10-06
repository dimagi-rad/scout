"""
Artifact views for Scout data agent platform.

Provides views for rendering artifacts in a sandboxed iframe,
fetching artifact data via API, and executing live queries.
"""

import hashlib
import json
import logging
import secrets
from typing import Any

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.views import View

from apps.artifacts.services.query_batch import (
    ArtifactQueryOutcome,
    execute_artifact_plan,
    plan_artifact_queries,
)
from apps.artifacts.services.recovery import (
    DISPATCH_FAILED_ERROR,
    Admission,
    admit_artifact_recovery,
    current_artifact_data_state,
)
from apps.common.capacity import CapacityExhausted, CapacityResource, reraise_if_capacity
from apps.common.http import parse_json_object
from apps.common.utils import creator_display_name
from apps.semantic.services.date_context import DateContextError, date_context
from apps.semantic.services.query import raise_if_capacity_exhausted
from apps.users.decorators import LoginRequiredJsonMixin
from apps.workspaces.models import WorkspaceRole
from apps.workspaces.workspace_resolver import aresolve_workspace, resolve_workspace

from .models import Artifact, ArtifactSemanticQuery, ArtifactType
from .services.data_export import (
    EXPORT_ROW_LIMIT,
    EXPORT_TIMEOUT_MESSAGE,
    EXPORT_TIMEOUT_SECONDS,
    audit_query_shape,
    export_datasets,
    export_error_response,
    export_filename,
    export_slot,
    find_planned_query,
    iter_csv,
    run_export_query,
    static_tabular_datasets,
)
from .services.export import ArtifactExporter
from .services.graph_manifest import (
    build_artifact_semantic_query_manifest,
    derive_missing_semantic_query_manifest,
    manifest_entry_summary,
    semantic_query_summary,
    sort_manifest_entries,
)
from .services.versioning import latest_visible_version_ids

logger = logging.getLogger(__name__)
# Pinned so a level change on apps.* cannot mute the data-export audit trail.
audit_logger = logging.getLogger("scout.export.audit")

# Short TTL for live-artifact query results (arch #254, finding 09#9). Live
# artifacts re-executed ALL their source queries serially on every open with no
# caching, so each viewer opening a 5-query dashboard cost 5 sequential
# connect+validate+execute cycles. A brief shared cache collapses repeat opens
# (the common case: a dashboard reloaded / shared with several viewers) onto one
# execution. The key includes the artifact version + a hash of the source
# queries, so an update invalidates it immediately.
ARTIFACT_QUERY_CACHE_TTL = 60  # seconds
ARTIFACT_QUERY_CONCURRENCY = 4


def _query_cache_intent(query):
    if isinstance(query, dict) and isinstance(query.get("query_context"), dict):
        return {
            **query,
            "query_context": {
                "timezone": query["query_context"].get("timezone", settings.TIME_ZONE)
            },
        }
    return query


def _artifact_query_cache_key(
    artifact: Artifact, data_revision: str = "", resolved_queries=None
) -> str:
    # A new clock instant with identical resolved bounds must not defeat the
    # short-lived cache. Timezone and the compiled date filters remain in it.
    if resolved_queries is not None:
        resolved_queries = [_query_cache_intent(query) for query in resolved_queries]
    payload = json.dumps(
        {
            "semantic_queries": artifact.semantic_queries,
            "source_queries": artifact.source_queries,
            "data_revision": data_revision,
            "resolved_queries": resolved_queries,
        },
        sort_keys=True,
        default=str,
    )
    digest = hashlib.md5(payload.encode(), usedforsecurity=False).hexdigest()[:12]
    return f"artifact_qdata:{artifact.id}:{artifact.version}:{digest}"


# Must match the sandbox attribute on the iframe in ArtifactCanvas.tsx. The CSP
# `sandbox` directive gives the document an opaque origin even when its URL is
# opened outside that iframe; allow-same-origin must never be added here.
SANDBOX_FLAGS = "allow-scripts allow-modals"


def generate_csp_with_nonce(nonce: str) -> str:
    """Build the sandbox CSP header allowing only nonce'd inline scripts.

    'unsafe-eval' stays because the renderer compiles artifact code at runtime
    (Babel for JSX, then `new Function` for React and D3 artifacts).
    """
    return (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}' 'unsafe-eval' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com; "
        "style-src 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src data: blob:; "
        "font-src https://cdn.jsdelivr.net; "
        "connect-src https://cdn.jsdelivr.net; "
        "base-uri 'none'; "
        "form-action 'none'; "
        # Mirrors X-Frame-Options: SAMEORIGIN below. Cross-origin embeds
        # (EMBED_ALLOWED_ORIGINS) would need their origins added here too.
        "frame-ancestors 'self'; "
        f"sandbox {SANDBOX_FLAGS};"
    )


SANDBOX_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Artifact Sandbox</title>

    <!-- Tailwind CSS -->
    <script nonce="{{CSP_NONCE}}" src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>

    <!-- React 18 -->
    <script nonce="{{CSP_NONCE}}" crossorigin src="https://cdn.jsdelivr.net/npm/react@18/umd/react.production.min.js"></script>
    <script nonce="{{CSP_NONCE}}" crossorigin src="https://cdn.jsdelivr.net/npm/react-dom@18/umd/react-dom.production.min.js"></script>

    <!-- Babel for JSX transformation -->
    <script nonce="{{CSP_NONCE}}" src="https://cdn.jsdelivr.net/npm/@babel/standalone@7/babel.min.js"></script>

    <!-- PropTypes (required by Recharts UMD) -->
    <script nonce="{{CSP_NONCE}}" src="https://cdn.jsdelivr.net/npm/prop-types@15/prop-types.min.js"></script>

    <!-- Recharts for React charts -->
    <script nonce="{{CSP_NONCE}}" src="https://cdn.jsdelivr.net/npm/recharts@2/umd/Recharts.min.js"></script>

    <!-- D3 for custom visualizations -->
    <script nonce="{{CSP_NONCE}}" src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>

    <!-- Lodash for data manipulation -->
    <script nonce="{{CSP_NONCE}}" src="https://cdn.jsdelivr.net/npm/lodash@4/lodash.min.js"></script>

    <!-- Lucide icons (referenced by agent-generated React code) -->
    <script nonce="{{CSP_NONCE}}" src="https://cdn.jsdelivr.net/npm/lucide@0.460.0/dist/umd/lucide.min.js"></script>

    <!-- Marked for Markdown rendering -->
    <script nonce="{{CSP_NONCE}}" src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>

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
        /* Print-to-PDF styling: white background, full content (no clipping),
           sensible page margins, and visible chart SVGs. */
        @media print {
            @page {
                size: auto;
                margin: 16mm;
            }
            html, body {
                height: auto;
                overflow: visible;
                background: #ffffff;
            }
            #root {
                height: auto;
                display: block;
            }
            #artifact-container {
                overflow: visible;
                height: auto;
                padding: 0;
            }
            /* The loading spinner is non-content chrome; never print it. */
            .loading-state {
                display: none !important;
            }
            /* Ensure chart colors/backgrounds render instead of being stripped. */
            * {
                -webkit-print-color-adjust: exact !important;
                print-color-adjust: exact !important;
            }
            /* Recharts draws into SVG/canvas; keep charts visible and unclipped. */
            svg, canvas {
                max-width: 100% !important;
                overflow: visible !important;
            }
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

    <!-- Artifact data injected by server -->
    <script id="artifact-data" type="application/json" nonce="{{CSP_NONCE}}">{{ARTIFACT_DATA}}</script>

    <script nonce="{{CSP_NONCE}}">
        // Artifact rendering system
        const ArtifactRenderer = {
            container: null,
            currentArtifact: null,

            async init() {
                this.container = document.getElementById('artifact-container');
                const dataEl = document.getElementById('artifact-data');
                if (!dataEl) {
                    this.showError('Initialization Error', 'No artifact data found in page.');
                    return;
                }

                let artifact;
                try {
                    artifact = JSON.parse(dataEl.textContent);
                } catch (error) {
                    this.showError('Parse Error', 'Failed to parse embedded artifact data: ' + error.message);
                    return;
                }

                this.render(artifact);
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
                        case 'svg':
                            this.renderSVG(artifact);
                            break;
                        default:
                            this.showError('Unknown artifact type', `Type "${artifact.type}" is not supported.`);
                    }
                } catch (error) {
                    const thrown = describeThrown(error);
                    this.showError('Render Error', thrown.message, thrown.stack, thrown.name);
                }
            },

            // Strip ES module syntax since all libraries are provided as globals
            stripModuleSyntax(code) {
                // Capture the name from 'export default function/class Name'
                // so we can alias it to _default_export afterwards
                const namedDefaultMatch = code.match(
                    /^export\\s+default\\s+(?:function|class)\\s+(\\w+)/m
                );

                let result = code
                    // Remove: import X from 'module', import { X } from 'module', import 'module'
                    .replace(/^\\s*import\\s+(?:[\\s\\S]*?)from\\s+['"][^'"]*['"]\\s*;?\\s*$/gm, '')
                    .replace(/^\\s*import\\s+['"][^'"]*['"]\\s*;?\\s*$/gm, '')
                    // export default function/class Name → just the declaration
                    .replace(/^(\\s*)export\\s+default\\s+(function|class)\\b/gm, '$1$2')
                    // export default const/let/var → just the declaration
                    .replace(/^(\\s*)export\\s+default\\s+(const|let|var)\\b/gm, '$1$2')
                    // export default Expression → const _default_export = Expression
                    .replace(/^(\\s*)export\\s+default\\s+/gm, '$1const _default_export = ')
                    // export function/class/const → just the declaration
                    .replace(/^(\\s*)export\\s+(function|class|const|let|var)\\b/gm, '$1$2');

                // Add alias so component discovery can find it by _default_export
                if (namedDefaultMatch) {
                    result += '\\nvar _default_export = ' + namedDefaultMatch[1] + ';';
                }

                return result;
            },

            renderReact(artifact) {
                const { code, data } = artifact;

                // Create a fresh container for React
                this.container.innerHTML = '<div id="react-root"></div>';
                const reactRoot = document.getElementById('react-root');

                try {
                    // Strip imports/exports then transform JSX using Babel
                    const stripped = this.stripModuleSyntax(code);
                    const transformed = Babel.transform(stripped, {
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
                        'lucide',
                        `
                        const { useState, useEffect, useRef, useMemo, useCallback, memo, Fragment } = React;
                        const {
                            // Charts
                            AreaChart, BarChart, ComposedChart, LineChart, PieChart,
                            RadarChart, RadialBarChart, ScatterChart, FunnelChart,
                            Treemap, Sankey,
                            // Series
                            Area, Bar, Line, Pie, Radar, RadialBar, Scatter, Funnel,
                            // Axes & grids
                            XAxis, YAxis, ZAxis, CartesianGrid, CartesianAxis,
                            PolarGrid, PolarAngleAxis, PolarRadiusAxis,
                            // Reference shapes
                            ReferenceLine, ReferenceArea, ReferenceDot,
                            // Decorations
                            Tooltip, Legend, Label, LabelList, Cell, Customized,
                            Brush, ErrorBar,
                            // Containers & primitives
                            ResponsiveContainer, Cross, Curve, Dot, Polygon,
                            Rectangle, Sector, Symbols, Trapezoid,
                            Layer, Surface, Text
                        } = Recharts;

                        // Lucide icon helper: creates a React component from a lucide icon name
                        function _lucideIcon(name) {
                            return function LucideIcon(props) {
                                const ref = React.useRef(null);
                                React.useEffect(() => {
                                    if (ref.current && lucide && lucide[name]) {
                                        const svg = lucide.createElement(lucide[name]);
                                        ref.current.innerHTML = '';
                                        ref.current.appendChild(svg);
                                        const svgEl = ref.current.querySelector('svg');
                                        if (svgEl) {
                                            if (props.size) { svgEl.setAttribute('width', props.size); svgEl.setAttribute('height', props.size); }
                                            if (props.style && props.style.color) svgEl.setAttribute('stroke', props.style.color);
                                            if (props.className) svgEl.setAttribute('class', props.className);
                                        }
                                    }
                                }, []);
                                return React.createElement('span', { ref: ref, style: { display: 'inline-flex', ...props.style } });
                            };
                        }
                        const TrendingUp = _lucideIcon('TrendingUp');
                        const TrendingDown = _lucideIcon('TrendingDown');
                        const ShoppingCart = _lucideIcon('ShoppingCart');
                        const DollarSign = _lucideIcon('DollarSign');
                        const Users = _lucideIcon('Users');
                        const Package = _lucideIcon('Package');
                        const BarChart3 = _lucideIcon('BarChart3');
                        const Activity = _lucideIcon('Activity');
                        const ArrowUp = _lucideIcon('ArrowUp');
                        const ArrowDown = _lucideIcon('ArrowDown');
                        const Star = _lucideIcon('Star');

                        ${transformed}

                        // Try to find the component: default export, or named App/Component/Chart/etc.
                        const _Component = typeof _default_export !== 'undefined' ? _default_export :
                                          typeof exports !== 'undefined' ? exports.default :
                                          typeof App !== 'undefined' ? App :
                                          typeof Chart !== 'undefined' ? Chart :
                                          typeof Visualization !== 'undefined' ? Visualization :
                                          typeof Dashboard !== 'undefined' ? Dashboard :
                                          typeof Report !== 'undefined' ? Report :
                                          typeof ReportCard !== 'undefined' ? ReportCard : null;
                        return _Component;
                        `
                    );

                    const Component = componentFactory(
                        React,
                        ReactDOM,
                        Recharts,
                        d3,
                        _,
                        data || {},
                        typeof lucide !== 'undefined' ? lucide : {}
                    );

                    if (Component) {
                        const root = ReactDOM.createRoot(reactRoot);
                        // Wrap in error boundary to catch render-time crashes
                        class _ErrorBoundary extends React.Component {
                            constructor(props) { super(props); this.state = { error: null }; }
                            static getDerivedStateFromError(error) { return { error: describeThrown(error) }; }
                            componentDidCatch(error) {
                                const thrown = describeThrown(error);
                                ArtifactRenderer.notifyParentOfError('React Render Error', thrown.message, thrown.stack, thrown.name);
                            }
                            render() {
                                if (this.state.error) {
                                    return React.createElement('div', { className: 'error-state' },
                                        React.createElement('div', { className: 'error-title' }, 'Render Error'),
                                        React.createElement('div', { className: 'error-message' }, this.state.error.message),
                                        React.createElement('div', { className: 'error-details' }, this.state.error.stack)
                                    );
                                }
                                return this.props.children;
                            }
                        }
                        root.render(React.createElement(_ErrorBoundary, null,
                            React.createElement(Component, { data: data || {} })
                        ));
                    } else {
                        this.showError('Component Not Found', 'Could not find a valid React component to render. Make sure your code exports a component or defines App, Component, Chart, or Visualization.');
                    }
                } catch (error) {
                    const thrown = describeThrown(error);
                    this.showError('React Render Error', thrown.message, thrown.stack, thrown.name);
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
                    const thrown = describeThrown(error);
                    this.showError('Markdown Render Error', thrown.message, null, thrown.name);
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
                    const thrown = describeThrown(error);
                    this.showError('SVG Render Error', thrown.message, thrown.stack, thrown.name);
                }
            },

            hideLoading() {
                const loading = document.getElementById('loading');
                if (loading) {
                    loading.style.display = 'none';
                }
            },

            showError(title, message, details = null, name = null) {
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
                this.notifyParentOfError(title, message, details, name);
            },

            // The parent reports these to Sentry, so they carry error text only,
            // never artifact data.
            notifyParentOfError(title, message, details = null, name = null) {
                // targetOrigin is '*' rather than the document origin: this
                // opaque-origin sandbox frame has window.location.origin equal
                // to the string 'null', which the browser rejects as a
                // postMessage target, so the message would never reach the
                // parent. The parent authenticates by event.source, so '*'
                // leaks nothing.
                try {
                    window.parent.postMessage({
                        type: 'artifact-error',
                        error: { title, message, details, name }
                    }, '*');
                } catch (e) { /* ignore if not in iframe */ }
            },

            escapeHtml(text) {
                const div = document.createElement('div');
                div.textContent = text;
                return div.innerHTML;
            }
        };

        // Generated code can also fail outside render (event handlers, timers,
        // promises), where neither the React boundary nor showError sees it.
        // A thrown or rejected non-Error may be a row the artifact loaded, and the
        // browser's own "Uncaught ..." text stringifies it. A string is a message by
        // intent, like an Error's, so it is kept; anything else is sent as its type.
        function nonErrorMessage(value, what) {
            return typeof value === 'string'
                ? value
                : `Non-Error ${what} (${value === null ? 'null' : typeof value})`;
        }
        // For the catch sites and React boundary, which get whatever artifact code threw.
        function describeThrown(thrown) {
            return thrown instanceof Error
                ? { message: thrown.message, stack: thrown.stack, name: thrown.name }
                : { message: nonErrorMessage(thrown, 'exception'), stack: null, name: null };
        }
        window.addEventListener('error', (event) => {
            const error = event.error;
            if (error instanceof Error) {
                ArtifactRenderer.notifyParentOfError(
                    'Uncaught Error', error.message, error.stack, error.name);
                return;
            }
            // A cross-origin "Script error." carries a null error; its text is safe.
            const message = error == null ? event.message : nonErrorMessage(error, 'exception');
            ArtifactRenderer.notifyParentOfError('Uncaught Error', message, null, null);
        });
        window.addEventListener('unhandledrejection', (event) => {
            const reason = event.reason;
            if (reason instanceof Error) {
                ArtifactRenderer.notifyParentOfError(
                    'Unhandled Rejection', reason.message, reason.stack, reason.name);
                return;
            }
            ArtifactRenderer.notifyParentOfError(
                'Unhandled Rejection', nonErrorMessage(reason, 'rejection'), null,
                'UnhandledRejection');
        });

        // Initialize when DOM is ready
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => ArtifactRenderer.init().catch(console.error));
        } else {
            ArtifactRenderer.init().catch(console.error);
        }

        // Print-to-PDF: the parent frame posts {type: 'scout-print'} to print
        // only the artifact (not the surrounding app). Triggering print inside
        // the sandboxed iframe scopes the print job to the artifact content.
        //
        // SECURITY: this iframe is sandboxed WITHOUT allow-same-origin, so its
        // document has a unique opaque ("null") security origin. An origin
        // allowlist is therefore broken here on BOTH ends: legitimate messages
        // from the real parent arrive with event.origin === the app's concrete
        // origin (never "null"), so an `event.origin === window.location.origin`
        // check would silently REJECT them and break Export PDF; and any other
        // sandboxed frame on the page also reports event.origin "null", so a
        // "null"-origin allowance would TRUST forgeries from sibling frames.
        // The robust gate is on the message source: only accept messages posted
        // by our actual parent window, mirroring the source-based check the
        // parent (ArtifactPanel) uses on inbound artifact messages.
        window.addEventListener('message', (event) => {
            if (event.source !== window.parent) return;
            if (event.data && event.data.type === 'scout-print') {
                window.print();
            }
        });
    </script>
</body>
</html>"""


class ArtifactSandboxView(LoginRequiredJsonMixin, View):
    """
    Serves the sandbox HTML template for rendering artifacts in an iframe.

    The sandbox page loads React, Recharts, D3, and other libraries
    from CDN and listens for postMessage events to render artifacts securely.
    """

    def get(self, request: HttpRequest, workspace_id, artifact_id: str) -> HttpResponse:
        """Return the sandbox HTML with strict CSP headers."""
        workspace, err = resolve_workspace(request.user, workspace_id)
        if err:
            return HttpResponse("Access denied", status=403)
        artifact = get_object_or_404(Artifact, pk=artifact_id, workspace=workspace)

        csp_nonce = secrets.token_urlsafe(16)

        artifact_json = json.dumps(
            {
                "id": str(artifact.id),
                "workspace_id": str(workspace_id),
                "title": artifact.title,
                "type": artifact.artifact_type,
                "code": artifact.code,
                "data": artifact.data or {},
                "version": artifact.version,
            }
        )
        # Escape </script> in JSON to prevent breaking out of the script tag
        artifact_json = artifact_json.replace("</", "<\\/")

        html_content = SANDBOX_HTML_TEMPLATE.replace("{{CSP_NONCE}}", csp_nonce)
        html_content = html_content.replace("{{ARTIFACT_DATA}}", artifact_json)

        response = HttpResponse(html_content, content_type="text/html")
        response["Content-Security-Policy"] = generate_csp_with_nonce(csp_nonce)
        response["X-Content-Type-Options"] = "nosniff"
        response["X-Frame-Options"] = "SAMEORIGIN"
        return response


class ArtifactDataView(LoginRequiredJsonMixin, View):
    """
    API endpoint to fetch artifact code and data.

    Returns JSON with artifact details for rendering in the sandbox.
    Requires project membership for access.
    """

    def get(self, request: HttpRequest, workspace_id, artifact_id: str) -> JsonResponse:
        workspace, err = resolve_workspace(request.user, workspace_id)
        if err:
            return err
        artifact = get_object_or_404(Artifact, pk=artifact_id, workspace=workspace)
        derive_missing_semantic_query_manifest(artifact)
        return JsonResponse(self._serialize_artifact(artifact))

    def _serialize_artifact(self, artifact: Artifact) -> dict[str, Any]:
        return {
            "id": str(artifact.id),
            "title": artifact.title,
            "type": artifact.artifact_type,
            "code": artifact.code,
            "data": artifact.data,
            "semantic_queries": artifact.semantic_queries,
            "semantic_query_manifest": artifact.semantic_query_manifest,
            "version": artifact.version,
            "date_context": date_context(),
        }


def _log_query_bugs(artifact: Artifact, outcomes) -> None:
    # A full pool is reported by CapacityExhausted's own rate-limited event.
    for outcome in outcomes:
        if outcome.exception is not None and outcome.capacity is None:
            logger.error(
                "Artifact query '%s' failed for artifact %s",
                outcome.planned.name,
                artifact.id,
                exc_info=outcome.exception,
            )


def _view_data_result(outcome: ArtifactQueryOutcome) -> dict[str, Any]:
    name = outcome.planned.name
    if outcome.executed is None:
        return {"name": name, "error": "Semantic query must be an object"}
    if outcome.exception is not None:
        return {"name": name, "semantic_query": outcome.executed, "error": "Semantic query failed"}
    result = outcome.result
    if not outcome.succeeded:
        error_info = result.get("error", {})
        msg = (
            error_info.get("message", "Semantic query failed")
            if isinstance(error_info, dict)
            else str(error_info)
        )
        return {"name": name, "semantic_query": outcome.executed, "error": msg}
    return {
        "name": name,
        "semantic_query": _query_cache_intent(result.get("semantic_query", outcome.executed)),
        "columns": result.get("columns", []),
        "rows": result.get("rows", []),
        "row_count": result.get("row_count", 0),
        "truncated": result.get("truncated", False),
    }


class ArtifactQueryDataView(View):
    """
    Executes an artifact's semantic_queries and returns results.

    Legacy SQL-backed ``source_queries`` are intentionally not executed.
    """

    async def post(self, request: HttpRequest, workspace_id, artifact_id: str) -> JsonResponse:
        return await self.get(request, workspace_id, artifact_id)

    async def get(self, request: HttpRequest, workspace_id, artifact_id: str) -> JsonResponse:
        user = await request.auser()
        if not user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=401)

        workspace, err = await aresolve_workspace(user, workspace_id)
        if err:
            return err

        runtime = None
        if request.method == "POST":
            runtime, err = parse_json_object(request, allow_empty=True)
            if err:
                return err

        try:
            artifact = await Artifact.objects.select_related("workspace").aget(
                pk=artifact_id, workspace=workspace
            )
        except Artifact.DoesNotExist:
            raise Http404 from None

        derive_missing_semantic_query_manifest(artifact)

        if not artifact.source_queries and not artifact.semantic_queries:
            return JsonResponse(
                {
                    "queries": [],
                    "static_data": artifact.data or {},
                    "semantic_query_manifest": artifact.semantic_query_manifest or {},
                }
            )

        if artifact.workspace is None:
            return JsonResponse({"error": "Artifact has no associated workspace"}, status=400)

        data_state = {}
        if artifact.semantic_queries:
            data_state = await current_artifact_data_state(artifact)
            if not data_state["queryable"]:
                return JsonResponse(
                    {
                        "error": data_state["message"],
                        "data_recovery": data_state,
                    },
                    status=409,
                )

        static_data = artifact.data or {}

        try:
            plan = plan_artifact_queries(artifact, runtime)
        except DateContextError as exc:
            logger.warning("Artifact %s date context rejected: %s", artifact.id, exc)
            return JsonResponse(
                {
                    "error": "Invalid artifact date context. Check the dates, timezone, and date-control bindings."
                },
                status=400,
            )
        resolved_context = plan.query_context

        # Serve repeat opens of the same artifact version from a short-lived
        # cache so we don't re-run every source query on every open (09#9).
        cache_key = _artifact_query_cache_key(
            artifact, data_state.get("data_revision", ""), plan.entries
        )
        cached = await cache.aget(cache_key)
        if cached is not None:
            return JsonResponse(
                {
                    "queries": cached,
                    "query_context": resolved_context,
                    "static_data": static_data,
                    "semantic_query_manifest": artifact.semantic_query_manifest or {},
                }
            )

        batch = await execute_artifact_plan(
            plan,
            artifact.workspace,
            user_id=str(user.id),
            row_limit=None,
            concurrency=ARTIFACT_QUERY_CONCURRENCY,
        )
        _log_query_bugs(artifact, batch.outcomes)
        # One full pool makes the whole panel retryable, rather than caching nothing
        # and rendering a per-chart error the user cannot act on.
        if batch.capacity is not None:
            raise CapacityExhausted(batch.capacity)
        results = [_view_data_result(outcome) for outcome in batch.outcomes]

        for i, entry in enumerate(artifact.source_queries):
            name = entry.get("name", f"query_{i}")
            results.append(
                {
                    "name": name,
                    "error": (
                        "Legacy SQL-backed artifact queries are disabled. "
                        "Recreate this artifact with semantic_queries."
                    ),
                }
            )

        if not any("error" in result for result in results):
            await cache.aset(cache_key, results, ARTIFACT_QUERY_CACHE_TTL)

        return JsonResponse(
            {
                "queries": results,
                "query_context": resolved_context,
                "static_data": static_data,
                "semantic_query_manifest": artifact.semantic_query_manifest or {},
            }
        )


class ArtifactDataRecoveryView(View):
    """Inspect or start repair of the data surface behind an artifact."""

    async def _resolve(
        self,
        request: HttpRequest,
        workspace_id,
        artifact_id,
        *,
        minimum_role: str = WorkspaceRole.READ,
    ):
        user = await request.auser()
        if not user.is_authenticated:
            return None, None, JsonResponse({"error": "Authentication required"}, status=401)
        workspace, err = await aresolve_workspace(user, workspace_id, minimum_role=minimum_role)
        if err:
            return None, None, err
        try:
            artifact = await Artifact.objects.select_related("workspace").aget(
                pk=artifact_id,
                workspace=workspace,
            )
        except Artifact.DoesNotExist:
            return None, None, JsonResponse({"error": "Artifact not found"}, status=404)
        derive_missing_semantic_query_manifest(artifact)
        return user, artifact, None

    async def get(self, request: HttpRequest, workspace_id, artifact_id) -> JsonResponse:
        _user, artifact, err = await self._resolve(request, workspace_id, artifact_id)
        if err:
            return err
        return JsonResponse(await current_artifact_data_state(artifact))

    async def post(self, request: HttpRequest, workspace_id, artifact_id) -> JsonResponse:
        user, artifact, err = await self._resolve(
            request,
            workspace_id,
            artifact_id,
            minimum_role=WorkspaceRole.READ_WRITE,
        )
        if err:
            return err
        admitted = await admit_artifact_recovery(artifact, user)
        if admitted.admission == Admission.UNRECOVERABLE:
            return JsonResponse(
                {"error": admitted.state["message"], "data_recovery": admitted.state},
                status=409,
            )
        if admitted.admission == Admission.FAILED:
            return JsonResponse({"error": DISPATCH_FAILED_ERROR}, status=500)
        status = 202 if admitted.admission == Admission.STARTED else 200
        return JsonResponse(admitted.state, status=status)


class ArtifactSemanticQueryView(LoginRequiredJsonMixin, View):
    """
    GET /api/workspaces/<workspace_id>/artifacts/<artifact_id>/semantic-queries/
    Returns paginated graph artifact semantic query dependencies.
    """

    def get(self, request: HttpRequest, workspace_id, artifact_id: str) -> JsonResponse:
        workspace, err = resolve_workspace(request.user, workspace_id)
        if err:
            return err
        artifact = get_object_or_404(Artifact, pk=artifact_id, workspace=workspace)
        limit = _bounded_int(request.GET.get("limit"), default=25, lower=1, upper=100)
        offset = _bounded_int(request.GET.get("offset"), default=0, lower=0, upper=100_000)
        if artifact.artifact_type == ArtifactType.STORY:
            # READ members can call this. Syncing here let any viewer rewrite shared
            # live-query metadata, and for a drifted catalog persist an empty
            # semantic_queries that cut off live data workspace-wide (see #515).
            manifest = build_artifact_semantic_query_manifest(artifact)
            entries = sort_manifest_entries(manifest["entries"])
            total_count = len(entries)
            records = [manifest_entry_summary(e) for e in entries[offset : offset + limit]]
        else:
            queryset = ArtifactSemanticQuery.objects.filter(artifact=artifact).order_by("query_key")
            total_count = queryset.count()
            records = [semantic_query_summary(r) for r in queryset[offset : offset + limit]]
            manifest = artifact.semantic_query_manifest or {}
        return JsonResponse(
            {
                "artifact": {
                    "id": str(artifact.id),
                    "title": artifact.title,
                    "version": artifact.version,
                    "artifact_type": artifact.artifact_type,
                },
                "semantic_queries": records,
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "count": len(records),
                    "total_count": total_count,
                    "has_more": offset + len(records) < total_count,
                },
                "manifest": {
                    "schema_version": manifest.get("schema_version"),
                    "generated_at": manifest.get("generated_at"),
                    "source": manifest.get("source"),
                    "entry_count": len(manifest.get("entries") or []),
                    "unresolved_count": len(manifest.get("unresolved") or []),
                    "unresolved": manifest.get("unresolved") or [],
                },
            }
        )


def _bounded_int(value: Any, *, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(parsed, upper))


class ArtifactListView(LoginRequiredJsonMixin, View):
    """
    GET /api/workspaces/<workspace_id>/artifacts/ - List each artifact's latest visible revision.
    """

    def get(self, request: HttpRequest, workspace_id) -> JsonResponse:
        workspace, err = resolve_workspace(request.user, workspace_id)
        if err:
            return err

        search = request.GET.get("search", "").strip()
        queryset = Artifact.objects.filter(
            workspace=workspace,
            artifact_type__in=ArtifactType.values,
            id__in=latest_visible_version_ids(workspace.id),
        )
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search) | Q(description__icontains=search)
            )

        results = [
            {
                "id": str(a.id),
                "title": a.title,
                "description": a.description,
                "artifact_type": a.artifact_type,
                "version": a.version,
                "has_live_queries": bool(a.semantic_queries),
                "created_by_name": creator_display_name(a.created_by),
                "created_at": a.created_at.isoformat(),
                "updated_at": a.updated_at.isoformat(),
            }
            for a in queryset.select_related("created_by")
        ]
        return JsonResponse({"results": results})


class ArtifactDetailView(LoginRequiredJsonMixin, View):
    """
    PATCH /api/artifacts/<workspace_id>/<artifact_id>/ - Update title/description.
    DELETE /api/artifacts/<workspace_id>/<artifact_id>/ - Delete artifact.
    """

    def _get_artifact_with_access(
        self,
        request: HttpRequest,
        workspace_id,
        artifact_id: str,
        *,
        minimum_role: str,
    ):
        workspace, err = resolve_workspace(request.user, workspace_id, minimum_role=minimum_role)
        if err:
            return None, err
        artifact = get_object_or_404(
            Artifact,
            pk=artifact_id,
            workspace=workspace,
            artifact_type__in=ArtifactType.values,
        )
        return artifact, None

    def patch(self, request: HttpRequest, workspace_id, artifact_id: str) -> JsonResponse:
        artifact, err = self._get_artifact_with_access(
            request,
            workspace_id,
            artifact_id,
            minimum_role=WorkspaceRole.READ_WRITE,
        )
        if err:
            return err
        data, err = parse_json_object(request)
        if err:
            return err
        update_fields = []
        if "title" in data:
            artifact.title = data["title"]
            update_fields.append("title")
        if "description" in data:
            artifact.description = data["description"]
            update_fields.append("description")
        if update_fields:
            update_fields.append("updated_at")
            artifact.save(update_fields=update_fields)
        return JsonResponse(
            {"id": str(artifact.id), "title": artifact.title, "description": artifact.description}
        )

    def delete(self, request: HttpRequest, workspace_id, artifact_id: str) -> HttpResponse:
        artifact, err = self._get_artifact_with_access(
            request,
            workspace_id,
            artifact_id,
            minimum_role=WorkspaceRole.READ_WRITE,
        )
        if err:
            return err
        artifact.soft_delete(deleted_by=request.user)
        return HttpResponse(status=204)


class ArtifactUndeleteView(LoginRequiredJsonMixin, View):
    """POST /api/artifacts/<workspace_id>/<artifact_id>/undelete/ — Restore a soft-deleted artifact."""

    def post(self, request: HttpRequest, workspace_id, artifact_id: str) -> JsonResponse:
        workspace, err = resolve_workspace(
            request.user, workspace_id, minimum_role=WorkspaceRole.READ_WRITE
        )
        if err:
            return err
        artifact = get_object_or_404(Artifact.all_objects, pk=artifact_id, workspace=workspace)
        artifact.undelete()
        return JsonResponse({"id": str(artifact.id), "is_deleted": False})


class ArtifactExportView(LoginRequiredJsonMixin, View):
    """
    Export artifacts as standalone HTML.

    Requires project membership for access.
    """

    def get(
        self, request: HttpRequest, workspace_id, artifact_id: str, format: str
    ) -> HttpResponse:
        """Export artifact to the given format (html)."""
        workspace, err = resolve_workspace(request.user, workspace_id)
        if err:
            return err
        artifact = get_object_or_404(Artifact, pk=artifact_id, workspace=workspace)

        if format != "html":
            return JsonResponse(
                {"error": f"Invalid format: {format}. Supported formats: html"},
                status=400,
            )

        exporter = ArtifactExporter(artifact)
        filename = exporter.get_download_filename(format)

        try:
            content = exporter.export_html()
        except ValueError as error:
            return JsonResponse({"error": str(error)}, status=400)
        response = HttpResponse(content, content_type="text/html")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


def _log_safe(value: str) -> str:
    return value.replace("\r", "\\r").replace("\n", "\\n")


class _ArtifactDataExportBase(View):
    """Shared access for the data export endpoints (#846).

    Downloading raw rows takes READ_WRITE: read-only members can view an
    artifact's charts but not take its data away. POST carries the same date
    runtime as query-data so the export matches what the viewer is looking at.
    """

    async def _resolve(self, request: HttpRequest, workspace_id, artifact_id):
        user = await request.auser()
        if not user.is_authenticated:
            return None, None, None, JsonResponse({"error": "Authentication required"}, status=401)
        workspace, err = await aresolve_workspace(
            user, workspace_id, minimum_role=WorkspaceRole.READ_WRITE
        )
        if err:
            return None, None, None, err
        runtime = None
        if request.method == "POST":
            runtime, err = parse_json_object(request, allow_empty=True)
            if err:
                return None, None, None, err
        try:
            artifact = await Artifact.objects.select_related("workspace").aget(
                pk=artifact_id, workspace=workspace
            )
        except Artifact.DoesNotExist:
            return None, None, None, JsonResponse({"error": "Artifact not found"}, status=404)
        derive_missing_semantic_query_manifest(artifact)
        try:
            plan = plan_artifact_queries(artifact, runtime)
        except DateContextError:
            return (
                None,
                None,
                None,
                JsonResponse({"error": "Invalid artifact date context."}, status=400),
            )
        return user, artifact, plan, None


class ArtifactDataExportListView(_ArtifactDataExportBase):
    """
    GET/POST /api/workspaces/<workspace_id>/artifacts/<artifact_id>/data-export/
    Lists the datasets an artifact can export, without running any query.
    """

    async def post(self, request: HttpRequest, workspace_id, artifact_id) -> JsonResponse:
        return await self.get(request, workspace_id, artifact_id)

    async def get(self, request: HttpRequest, workspace_id, artifact_id) -> JsonResponse:
        _user, artifact, plan, err = await self._resolve(request, workspace_id, artifact_id)
        if err:
            return err
        return JsonResponse(
            {"datasets": export_datasets(plan, artifact.data), "row_limit": EXPORT_ROW_LIMIT}
        )


class ArtifactDataExportCsvView(_ArtifactDataExportBase):
    """
    POST /api/workspaces/<workspace_id>/artifacts/<artifact_id>/data-export/csv/
        ?query=<name> | ?static=<key>

    Streams one dataset as CSV, capped at EXPORT_ROW_LIMIT rows. A capped export
    says so in X-Scout-Export-Truncated and in its filename, never inside the data.
    POST only: prod's session cookie is SameSite=None for embeds, so a GET would
    let any cross-site page fire 50k-row Cube queries as the viewer; POST gets CSRF.
    """

    http_method_names = ["post", "options"]

    async def post(self, request: HttpRequest, workspace_id, artifact_id) -> HttpResponse:
        user, artifact, plan, err = await self._resolve(request, workspace_id, artifact_id)
        if err:
            return err
        query_name = request.GET.get("query")
        static_key = request.GET.get("static")
        if (query_name is None) == (static_key is None):
            return JsonResponse(
                {"error": "Name exactly one dataset with ?query= or ?static=."}, status=400
            )

        if static_key is not None:
            table = static_tabular_datasets(artifact.data).get(static_key)
            if table is None:
                return JsonResponse({"error": "Dataset not found"}, status=404)
            return await self._csv_response(
                user,
                artifact,
                dataset=static_key,
                source="static",
                columns=table.columns,
                rows=table.rows[:EXPORT_ROW_LIMIT],
                truncated=len(table.rows) > EXPORT_ROW_LIMIT,
                query=None,
            )

        planned = find_planned_query(plan, query_name)
        if planned is None:
            return JsonResponse({"error": "Dataset not found"}, status=404)
        data_state = await current_artifact_data_state(artifact)
        if not data_state["queryable"]:
            return JsonResponse(
                {"error": data_state["message"], "data_recovery": data_state}, status=409
            )
        slot = export_slot()
        if slot is None:
            raise CapacityExhausted(CapacityResource.CUBE, "Artifact data export slots are full")
        try:
            with slot:
                result = await run_export_query(artifact.workspace, planned, user_id=str(user.id))
        except TimeoutError:
            logger.warning(
                "Artifact data export timed out after %ss: artifact=%s query=%s",
                EXPORT_TIMEOUT_SECONDS,
                artifact.id,
                _log_safe(query_name),
            )
            return JsonResponse({"error": EXPORT_TIMEOUT_MESSAGE}, status=504)
        except Exception as exc:
            reraise_if_capacity(exc)
            logger.exception("Artifact data export failed: artifact=%s", artifact.id)
            return JsonResponse({"error": "The export query failed."}, status=500)
        raise_if_capacity_exhausted(result)
        error = result.get("error")
        if error:
            return export_error_response(error)
        return await self._csv_response(
            user,
            artifact,
            dataset=query_name,
            source="query",
            columns=result.get("columns", []),
            rows=result.get("rows", []),
            truncated=bool(result.get("truncated")),
            query=result.get("semantic_query"),
        )

    async def _csv_response(
        self, user, artifact, *, dataset, source, columns, rows, truncated, query
    ) -> StreamingHttpResponse:
        # With CONN_MAX_AGE=0 the request's connection otherwise stays open until
        # request_finished, which under ASGI fires after the last streamed byte.
        await sync_to_async(close_old_connections)()
        audit_logger.info(
            "Artifact data export started: user=%s workspace=%s artifact=%s source=%s dataset=%r "
            "rows=%d truncated=%s query=%s",
            user.id,
            artifact.workspace_id,
            artifact.id,
            source,
            _log_safe(dataset),
            len(rows),
            truncated,
            json.dumps(audit_query_shape(query), sort_keys=True) if query is not None else "-",
        )
        filename = export_filename(artifact.title, dataset, truncated=truncated)
        response = StreamingHttpResponse(
            iter_csv(columns, rows), content_type="text/csv; charset=utf-8"
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Cache-Control"] = "no-store"
        response["X-Scout-Export-Row-Count"] = str(len(rows))
        response["X-Scout-Export-Row-Limit"] = str(EXPORT_ROW_LIMIT)
        response["X-Scout-Export-Truncated"] = "true" if truncated else "false"
        return response
