"""
Scout MCP Server.

Exposes Scout project tools (SQL queries, table descriptions, etc.) via the
Model Context Protocol, allowing external AI agents to interact with Scout
projects.

Usage:
    # stdio transport (for Claude Desktop / local clients)
    python -m mcp_server

    # HTTP transport (for networked clients)
    python -m mcp_server --transport streamable-http

    # Specify host/port for HTTP
    python -m mcp_server --transport streamable-http --host 0.0.0.0 --port 9000
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Django setup must happen before importing any models
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")

import django  # noqa: E402

django.setup()

from mcp.server.fastmcp import FastMCP  # noqa: E402

from mcp_server.tools.describe_table import register_describe_table  # noqa: E402
from mcp_server.tools.list_projects import register_list_projects  # noqa: E402
from mcp_server.tools.sql import register_sql_tool  # noqa: E402

logger = logging.getLogger(__name__)

mcp = FastMCP("scout")


def _configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,  # never write to stdout with stdio transport
    )


def _register_tools() -> None:
    """Register all MCP tools on the server instance."""
    register_list_projects(mcp)
    register_sql_tool(mcp)
    register_describe_table(mcp)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8100, help="HTTP port (default: 8100)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    _configure_logging(args.verbose)
    _register_tools()

    logger.info("Starting Scout MCP server (transport=%s)", args.transport)

    kwargs: dict = {"transport": args.transport}
    if args.transport == "streamable-http":
        kwargs["host"] = args.host
        kwargs["port"] = args.port

    mcp.run(**kwargs)


if __name__ == "__main__":
    main()
