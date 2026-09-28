# Getting started

This section walks you through installing Scout and connecting it to your CommCare data.

## Prerequisites

- Python 3.12+ (3.11+ supported)
- Docker Compose for PostgreSQL 16 and the Cube runtime/schema validator
- Node.js 18+ or [Bun](https://bun.sh/)
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- An [Anthropic API key](https://console.anthropic.com/)

Scout's background jobs use Procrastinate with PostgreSQL. Redis is not required.

## Steps

1. **[Installation](installation.md)** -- Install dependencies and start the backend and frontend servers.
2. **[Testing CommCare integration in development](dev-testing.md)** -- Connect a CommCare domain using an API key or OAuth flow, then run materialization.
3. **[Start your first conversation](first-conversation.md)** -- Ask a question and explore the results.
