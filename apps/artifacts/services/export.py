"""
Artifact export service for HTML, PNG, and PDF output.

This module provides the ArtifactExporter class which can export artifacts
to various formats for download or sharing.
"""

from __future__ import annotations

import base64
import json
import logging
from io import BytesIO
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.artifacts.models import Artifact

logger = logging.getLogger(__name__)

# Standalone HTML template with embedded libraries (from CDN)
STANDALONE_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <script src="https://cdn.jsdelivr.net/npm/react@18/umd/react.production.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/react-dom@18/umd/react-dom.production.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/@babel/standalone/babel.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/recharts@2/umd/Recharts.js"></script>
    <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
    <script src="https://cdn.jsdelivr.net/npm/lodash@4/lodash.min.js"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body {{
            margin: 0;
            padding: 16px;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        }}
        #root {{
            width: 100%;
            height: 100%;
        }}
        .error {{
            color: #ef4444;
            padding: 16px;
            background: #fef2f2;
            border-radius: 8px;
            border: 1px solid #fee2e2;
        }}
    </style>
</head>
<body>
    <div id="root"></div>

    <script id="artifact-data" type="application/json">
    {data_json}
    </script>

    <script type="text/babel">
    {code}
    </script>

    <script type="text/babel">
        // Get data from embedded JSON
        const dataElement = document.getElementById('artifact-data');
        const data = JSON.parse(dataElement.textContent || '{{}}');

        // Render the component
        const root = ReactDOM.createRoot(document.getElementById('root'));
        try {{
            root.render(<App data={{data}} />);
        }} catch (e) {{
            root.render(<div className="error">Error rendering: {{e.message}}</div>);
        }}
    </script>
</body>
</html>
"""

MARKDOWN_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body {{
            margin: 0;
            padding: 32px;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            max-width: 800px;
            margin: 0 auto;
        }}
        .markdown-content {{
            padding: 16px;
        }}
        .markdown-content h1 {{ font-size: 2em; font-weight: bold; margin-top: 1em; }}
        .markdown-content h2 {{ font-size: 1.5em; font-weight: bold; margin-top: 1em; }}
        .markdown-content h3 {{ font-size: 1.25em; font-weight: bold; margin-top: 1em; }}
        .markdown-content p {{ margin: 1em 0; }}
        .markdown-content ul, .markdown-content ol {{ margin: 1em 0; padding-left: 2em; }}
        .markdown-content li {{ margin: 0.5em 0; }}
        .markdown-content code {{ background: #f1f5f9; padding: 2px 6px; border-radius: 4px; }}
        .markdown-content pre {{ background: #f1f5f9; padding: 16px; border-radius: 8px; overflow-x: auto; }}
        .markdown-content blockquote {{ border-left: 4px solid #e2e8f0; padding-left: 16px; margin: 1em 0; color: #64748b; }}
        .markdown-content table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
        .markdown-content th, .markdown-content td {{ border: 1px solid #e2e8f0; padding: 8px 12px; text-align: left; }}
        .markdown-content th {{ background: #f8fafc; font-weight: bold; }}
    </style>
</head>
<body>
    <div id="content" class="markdown-content"></div>
    <script>
        const markdown = {markdown_json};
        document.getElementById('content').innerHTML = marked.parse(markdown);
    </script>
</body>
</html>
"""

PLOTLY_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
    <style>
        body {{
            margin: 0;
            padding: 16px;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        }}
        #chart {{
            width: 100%;
            height: 100vh;
            max-height: 800px;
        }}
    </style>
</head>
<body>
    <div id="chart"></div>
    <script>
        const spec = {plotly_json};
        Plotly.newPlot('chart', spec.data, spec.layout || {{}}, {{responsive: true}});
    </script>
</body>
</html>
"""


class ArtifactExporter:
    """
    Export artifacts to various formats.

    Supports:
    - HTML: Standalone HTML file with embedded libraries and data
    - PNG: Screenshot of the rendered artifact (requires playwright)
    - PDF: PDF version of the artifact (requires playwright)
    """

    def __init__(self, artifact: Artifact):
        """
        Initialize the exporter with an artifact.

        Args:
            artifact: The Artifact instance to export
        """
        self.artifact = artifact

    def export_html(self) -> str:
        """
        Export the artifact as a standalone HTML file.

        Returns:
            HTML string with embedded libraries and data
        """
        artifact = self.artifact

        if artifact.artifact_type == "markdown":
            return MARKDOWN_HTML_TEMPLATE.format(
                title=artifact.title or "Artifact",
                markdown_json=json.dumps(artifact.code),
            )

        if artifact.artifact_type == "plotly":
            # For plotly, the code contains the plot specification
            return PLOTLY_HTML_TEMPLATE.format(
                title=artifact.title or "Chart",
                plotly_json=artifact.code,
            )

        if artifact.artifact_type == "svg":
            # SVG can be embedded directly
            return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{artifact.title or 'SVG'}</title>
    <style>
        body {{
            margin: 0;
            padding: 16px;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
        }}
        svg {{
            max-width: 100%;
            height: auto;
        }}
    </style>
</head>
<body>
    {artifact.code}
</body>
</html>
"""

        # Default: React/JSX artifact
        return STANDALONE_HTML_TEMPLATE.format(
            title=artifact.title or "Artifact",
            data_json=json.dumps(artifact.data or {}),
            code=artifact.code,
        )

    async def export_png(self, width: int = 1200, height: int = 800) -> bytes:
        """
        Export the artifact as a PNG image.

        Requires playwright to be installed.

        Args:
            width: Image width in pixels
            height: Image height in pixels

        Returns:
            PNG image bytes
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise ImportError(
                "playwright is required for PNG export. Install with: pip install playwright && playwright install chromium"
            ) from e

        html_content = self.export_html()

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": width, "height": height})

            # Load the HTML content
            await page.set_content(html_content)

            # Wait for any JavaScript to render
            await page.wait_for_timeout(1000)

            # Take screenshot
            screenshot = await page.screenshot(type="png", full_page=False)

            await browser.close()

        return screenshot

    async def export_pdf(self, width: int = 1200, height: int = 800) -> bytes:
        """
        Export the artifact as a PDF.

        Requires playwright to be installed.

        Args:
            width: Page width in pixels
            height: Page height in pixels

        Returns:
            PDF bytes
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise ImportError(
                "playwright is required for PDF export. Install with: pip install playwright && playwright install chromium"
            ) from e

        html_content = self.export_html()

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": width, "height": height})

            # Load the HTML content
            await page.set_content(html_content)

            # Wait for any JavaScript to render
            await page.wait_for_timeout(1000)

            # Generate PDF
            pdf = await page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "1cm", "bottom": "1cm", "left": "1cm", "right": "1cm"},
            )

            await browser.close()

        return pdf

    def get_download_filename(self, format: str) -> str:
        """
        Get an appropriate filename for the export.

        Args:
            format: Export format (html, png, pdf)

        Returns:
            Filename string
        """
        title = self.artifact.title or "artifact"
        # Clean the title for use as a filename
        clean_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)
        clean_title = clean_title.strip().replace(" ", "_")[:50]
        return f"{clean_title}.{format}"


__all__ = ["ArtifactExporter"]
