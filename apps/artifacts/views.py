"""
Artifact views for Scout data agent platform.

Provides views for rendering artifacts in a sandboxed iframe,
fetching artifact data via API, and executing live queries.
"""

import json
import logging
import secrets
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from apps.common.utils import creator_display_name
from apps.semantic.services.query import run_semantic_query
from apps.users.decorators import LoginRequiredJsonMixin
from apps.workspaces.workspace_resolver import aresolve_workspace, resolve_workspace

from .models import Artifact
from .services.export import ArtifactExporter

logger = logging.getLogger(__name__)


def generate_csp_with_nonce(nonce: str) -> str:
    """
    Generate Content Security Policy with nonce for inline scripts.

    Args:
        nonce: A cryptographically secure random nonce.

    Returns:
        CSP header string with nonce for script-src.
    """
    return (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}' 'unsafe-eval' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com; "
        "style-src 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src data: blob:; "
        "font-src https://cdn.jsdelivr.net; "
        "connect-src 'self' https://cdn.jsdelivr.net;"
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

    <!-- Plotly for advanced charts -->
    <script nonce="{{CSP_NONCE}}" src="https://cdn.jsdelivr.net/npm/plotly.js-dist@2/plotly.min.js"></script>

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
            /* Recharts/Plotly draw into SVG/canvas; keep them visible and unclipped. */
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
        // Base path Scout is mounted under (e.g. "/scout" on the labs deploy via
        // FORCE_SCRIPT_NAME, "" at the root). Injected server-side so the
        // in-iframe live-query fetch resolves under the deploy prefix instead of
        // the host root (issue #248, 04#8b).
        const API_BASE = "{{API_BASE}}";
    </script>

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

                // If the artifact has live queries, fetch fresh data from the server.
                //
                // KNOWN LIMITATION (pre-existing, out of scope for the postMessage
                // fix below): this frame is sandboxed WITHOUT allow-same-origin, so
                // its document has an opaque ("null") origin. A fetch() from a
                // null-origin document is treated as cross-origin, so the browser
                // requires CORS on the response; /query-data returns no
                // Access-Control-Allow-Origin header, so the response is BLOCKED
                // ("from origin 'null' has been blocked by CORS policy"), regardless
                // of credentials. The artifact's embedded data is also {} for live
                // queries (see ArtifactSandboxView), so a live-query artifact cannot
                // self-hydrate inside the iframe and will show "Data Fetch Error".
                // The parent ArtifactPanel still loads live data for its Data tab via
                // its own same-origin api.get(); fixing the iframe VIEW tab needs a
                // separate change (e.g. embed query results server-side, or add CORS)
                // and is deliberately not attempted here.
                if (artifact.has_live_queries) {
                    this.showLoading('Querying database...');
                    try {
                        const resp = await fetch(API_BASE + '/api/workspaces/' + artifact.workspace_id + '/artifacts/' + artifact.id + '/query-data/', {
                            credentials: 'include',
                        });
                        if (!resp.ok) {
                            const err = await resp.json().catch(() => ({}));
                            throw new Error(err.error || 'Query failed with status ' + resp.status);
                        }
                        const queryData = await resp.json();
                        artifact.data = this.mergeQueryResults(queryData, artifact.data || {});
                        // Expose raw query info for parent frame (Data tab).
                        // targetOrigin is '*' rather than the document origin:
                        // this frame is sandboxed WITHOUT allow-same-origin, so
                        // its document has an opaque origin whose
                        // window.location.origin is the string 'null'. Passing
                        // 'null' as targetOrigin is rejected by the browser
                        // ("Invalid target origin 'null'"), and any concrete
                        // origin would not match the parent's, so the message
                        // would never be delivered. '*' is safe because the
                        // parent (ArtifactPanel) authenticates inbound messages by
                        // event.source === this iframe's contentWindow, not origin.
                        artifact._queryResults = queryData;
                        window.parent.postMessage({
                            type: 'artifact-query-data',
                            artifactId: artifact.id,
                            queryData: queryData,
                        }, '*');
                    } catch (error) {
                        this.showError('Data Fetch Error', error.message);
                        return;
                    }
                }

                this.render(artifact);
            },

            mergeQueryResults(queryData, staticData) {
                const queries = queryData.queries || [];
                if (queries.length === 0) return staticData;

                const merged = { ...staticData };
                for (const q of queries) {
                    if (q.error) continue;
                    // Key by query name so the component can access data.kpis, data.monthly, etc.
                    const rows = (q.rows || []).map(row => {
                        const obj = {};
                        (q.columns || []).forEach((col, i) => { obj[col] = row[i]; });
                        return obj;
                    });
                    // Always expose as array so components can reliably call .map()
                    merged[q.name] = rows;
                }
                return merged;
            },

            showLoading(message) {
                const loading = document.getElementById('loading');
                if (loading) {
                    loading.style.display = 'flex';
                    const span = loading.querySelector('span');
                    if (span) span.textContent = message || 'Loading...';
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
                        case 'story':
                            this.renderStory(artifact);
                            break;
                        default:
                            this.showError('Unknown artifact type', `Type "${artifact.type}" is not supported.`);
                    }
                } catch (error) {
                    this.showError('Render Error', error.message, error.stack);
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
                            static getDerivedStateFromError(error) { return { error }; }
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

            renderStory(artifact) {
                const storyDoc = artifact.data && artifact.data.story_doc;
                if (!storyDoc || !Array.isArray(storyDoc.blocks)) {
                    this.showError('Story Not Found', 'This story artifact has no story_doc.blocks payload.');
                    return;
                }

                const visibleBlocks = storyDoc.blocks.filter(block => !block.hidden);
                this.container.innerHTML = `
                    <article class="story-root" style="font-family: system-ui, -apple-system, sans-serif; color: #111827; max-width: 960px; margin: 0 auto;">
                        ${visibleBlocks.map(block => this.renderStoryBlock(block, artifact.data || {})).join('')}
                    </article>
                `;
            },

            renderStoryBlock(block, data) {
                const config = block.config || {};
                if (block.type === 'markdown') {
                    const body = config.body || '';
                    return `<section style="margin-bottom: 20px;" data-block-id="${this.escapeHtml(block.id || '')}">${marked.parse(body)}</section>`;
                }
                if (block.type === 'stat') {
                    const queryName = config.query;
                    const rows = Array.isArray(data[queryName]) ? data[queryName] : [];
                    const field = config.field;
                    const value = rows[0] && field ? rows[0][field] : config.value;
                    return `
                        <section style="margin-bottom: 16px; border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px;" data-block-id="${this.escapeHtml(block.id || '')}">
                            <div style="font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: .04em;">${this.escapeHtml(config.label || block.id || 'Metric')}</div>
                            <div style="font-size: 32px; font-weight: 700; margin-top: 4px;">${this.escapeHtml(value == null ? '—' : String(value))}</div>
                        </section>
                    `;
                }
                if (block.type === 'table') {
                    const queryName = config.query;
                    const rows = Array.isArray(data[queryName]) ? data[queryName] : [];
                    const columns = config.columns || (rows[0] ? Object.keys(rows[0]) : []);
                    return `
                        <section style="margin-bottom: 20px;" data-block-id="${this.escapeHtml(block.id || '')}">
                            ${config.title ? `<h2 style="font-size: 18px; margin: 0 0 8px;">${this.escapeHtml(config.title)}</h2>` : ''}
                            <div style="overflow: auto; border: 1px solid #e5e7eb; border-radius: 8px;">
                                <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
                                    <thead style="background: #f9fafb;">
                                        <tr>${columns.map(col => `<th style="text-align: left; padding: 8px; border-bottom: 1px solid #e5e7eb;">${this.escapeHtml(String(col))}</th>`).join('')}</tr>
                                    </thead>
                                    <tbody>
                                        ${rows.map(row => `<tr>${columns.map(col => `<td style="padding: 8px; border-bottom: 1px solid #f3f4f6;">${this.escapeHtml(row[col] == null ? '' : String(row[col]))}</td>`).join('')}</tr>`).join('')}
                                    </tbody>
                                </table>
                            </div>
                        </section>
                    `;
                }
                return `
                    <section style="margin-bottom: 16px; color: #6b7280;" data-block-id="${this.escapeHtml(block.id || '')}">
                        Unsupported story block: ${this.escapeHtml(block.type || 'unknown')}
                    </section>
                `;
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

                // Notify parent of error (if embedded in iframe).
                // targetOrigin is '*' rather than the document origin, for the
                // same reason as artifact-query-data above: this opaque-origin
                // sandbox frame has window.location.origin equal to the string
                // 'null', which the browser rejects as a postMessage target, so
                // the message would never reach the parent. The parent
                // authenticates by event.source, so '*' leaks nothing.
                try {
                    window.parent.postMessage({
                        type: 'artifact-error',
                        error: { title, message, details }
                    }, '*');
                } catch (e) { /* ignore if not in iframe */ }
            },

            escapeHtml(text) {
                const div = document.createElement('div');
                div.textContent = text;
                return div.innerHTML;
            }
        };

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

    The sandbox page loads React, Recharts, Plotly, D3, and other libraries
    from CDN and listens for postMessage events to render artifacts securely.
    """

    def get(self, request: HttpRequest, workspace_id, artifact_id: str) -> HttpResponse:
        """Return the sandbox HTML with strict CSP headers."""
        workspace, err = resolve_workspace(request.user, workspace_id)
        if err:
            return HttpResponse("Access denied", status=403)
        artifact = get_object_or_404(Artifact, pk=artifact_id, workspace=workspace)

        # Generate CSP nonce for inline scripts
        csp_nonce = secrets.token_urlsafe(16)

        has_live_queries = bool(artifact.semantic_queries)

        # Serialize artifact data for embedding in the template
        artifact_json = json.dumps(
            {
                "id": str(artifact.id),
                "workspace_id": str(workspace_id),
                "title": artifact.title,
                "type": artifact.artifact_type,
                "code": artifact.code,
                "data": artifact.data or {},
                "has_live_queries": has_live_queries,
                "version": artifact.version,
            }
        )
        # Escape </script> in JSON to prevent breaking out of the script tag
        artifact_json = artifact_json.replace("</", "<\\/")

        # Base prefix Scout is mounted under (FORCE_SCRIPT_NAME on the labs
        # deploy → SCRIPT_NAME in the request meta). Trailing slash trimmed so
        # the in-iframe fetch builds "<prefix>/api/..." without a double slash.
        api_base = request.META.get("SCRIPT_NAME", "").rstrip("/")

        # Inject the nonce, base prefix, and artifact data into the template.
        html_content = SANDBOX_HTML_TEMPLATE.replace("{{CSP_NONCE}}", csp_nonce)
        html_content = html_content.replace("{{API_BASE}}", api_base)
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
        """Fetch artifact data for rendering."""
        workspace, err = resolve_workspace(request.user, workspace_id)
        if err:
            return err
        artifact = get_object_or_404(Artifact, pk=artifact_id, workspace=workspace)
        return JsonResponse(self._serialize_artifact(artifact))

    def _serialize_artifact(self, artifact: Artifact) -> dict[str, Any]:
        """Serialize artifact for JSON response."""
        return {
            "id": str(artifact.id),
            "title": artifact.title,
            "type": artifact.artifact_type,
            "code": artifact.code,
            "data": artifact.data,
            "semantic_queries": artifact.semantic_queries,
            "version": artifact.version,
        }


def _json_safe(value: Any) -> Any:
    """Coerce database result values to JSON-serializable types."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


class ArtifactQueryDataView(View):
    """
    Executes an artifact's semantic_queries and returns results.

    Legacy SQL-backed ``source_queries`` are intentionally not executed. Results
    are returned in a format the artifact sandbox can consume directly via
    mergeQueryResults().
    """

    async def get(self, request: HttpRequest, workspace_id, artifact_id: str) -> JsonResponse:
        user = await request.auser()
        if not user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=401)

        workspace, err = await aresolve_workspace(user, workspace_id)
        if err:
            return err

        try:
            artifact = await Artifact.objects.select_related("workspace").aget(
                pk=artifact_id, workspace=workspace
            )
        except Artifact.DoesNotExist:
            raise Http404 from None

        if not artifact.source_queries and not artifact.semantic_queries:
            return JsonResponse({"queries": [], "static_data": artifact.data or {}})

        if artifact.workspace is None:
            return JsonResponse({"error": "Artifact has no associated workspace"}, status=400)

        results = []
        for i, entry in enumerate(artifact.semantic_queries):
            name = entry.get("name", f"semantic_query_{i}")
            query_spec = {k: v for k, v in entry.items() if k != "name"}
            result = await run_semantic_query(artifact.workspace, query_spec)

            if not result.get("success", True) or result.get("error"):
                error_info = result.get("error", {})
                msg = (
                    error_info.get("message", "Semantic query failed")
                    if isinstance(error_info, dict)
                    else str(error_info)
                )
                results.append({"name": name, "semantic_query": query_spec, "error": msg})
            else:
                results.append(
                    {
                        "name": name,
                        "semantic_query": result.get("semantic_query", query_spec),
                        "columns": result.get("columns", []),
                        "rows": result.get("rows", []),
                        "row_count": result.get("row_count", 0),
                        "truncated": result.get("truncated", False),
                    }
                )

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

        return JsonResponse({"queries": results, "static_data": artifact.data or {}})


class ArtifactListView(LoginRequiredJsonMixin, View):
    """
    GET /api/artifacts/<workspace_id>/ - List artifacts for the specified workspace.
    """

    def get(self, request: HttpRequest, workspace_id) -> JsonResponse:
        workspace, err = resolve_workspace(request.user, workspace_id)
        if err:
            return err

        search = request.GET.get("search", "").strip()
        queryset = Artifact.objects.filter(workspace=workspace)
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

    def _get_artifact_with_access(self, request: HttpRequest, workspace_id, artifact_id: str):
        workspace, err = resolve_workspace(request.user, workspace_id)
        if err:
            return None, err
        artifact = get_object_or_404(Artifact, pk=artifact_id, workspace=workspace)
        return artifact, None

    def patch(self, request: HttpRequest, workspace_id, artifact_id: str) -> JsonResponse:
        artifact, err = self._get_artifact_with_access(request, workspace_id, artifact_id)
        if err:
            return err
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "Invalid JSON"}, status=400)
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
        artifact, err = self._get_artifact_with_access(request, workspace_id, artifact_id)
        if err:
            return err
        artifact.soft_delete(deleted_by=request.user)
        return HttpResponse(status=204)


class ArtifactUndeleteView(LoginRequiredJsonMixin, View):
    """POST /api/artifacts/<workspace_id>/<artifact_id>/undelete/ — Restore a soft-deleted artifact."""

    def post(self, request: HttpRequest, workspace_id, artifact_id: str) -> JsonResponse:
        workspace, err = resolve_workspace(request.user, workspace_id)
        if err:
            return err
        artifact = get_object_or_404(Artifact.all_objects, pk=artifact_id, workspace=workspace)
        artifact.undelete()
        return JsonResponse({"id": str(artifact.id), "is_deleted": False})


class ArtifactExportView(LoginRequiredJsonMixin, View):
    """
    Export artifacts to various formats (HTML, PNG, PDF).

    Requires project membership for access.
    """

    def get(
        self, request: HttpRequest, workspace_id, artifact_id: str, format: str
    ) -> HttpResponse:
        """
        Export artifact to the specified format.

        Args:
            request: HTTP request
            workspace_id: UUID of the TenantMembership
            artifact_id: UUID of the artifact
            format: Export format (html, png, pdf)

        Returns:
            HttpResponse with the exported content
        """
        workspace, err = resolve_workspace(request.user, workspace_id)
        if err:
            return err
        artifact = get_object_or_404(Artifact, pk=artifact_id, workspace=workspace)

        # Validate format
        if format not in ("html", "png", "pdf"):
            return JsonResponse(
                {"error": f"Invalid format: {format}. Supported formats: html, png, pdf"},
                status=400,
            )

        exporter = ArtifactExporter(artifact)
        filename = exporter.get_download_filename(format)

        if format == "html":
            content = exporter.export_html()
            response = HttpResponse(content, content_type="text/html")
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            return response

        # PNG and PDF require async - return error for now
        # In production, this would use async views or background tasks
        if format in ("png", "pdf"):
            return JsonResponse(
                {
                    "error": f"{format.upper()} export requires an async endpoint. Use /api/artifacts/{artifact_id}/export/{format}/ with async support."
                },
                status=501,
            )

        return JsonResponse({"error": "Export failed"}, status=500)
