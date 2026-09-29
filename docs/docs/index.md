# Scout

Scout is an AI-powered data agent platform that lets teams query their data using natural language. Connect a CommCare, CommCare Connect, or Open Chat Studio account, load its data into a workspace, ask questions in plain English, and get answers as tables, charts, and interactive dashboards — no SQL required.

## Quick start

1. [Install Scout](getting-started/installation.md) — set up the backend and frontend
2. [Connect CommCare](getting-started/dev-testing.md) — link a CommCare domain and load its data
3. [Start a conversation](getting-started/first-conversation.md) — ask a question and explore the results

## Features

**Natural language data querying** — Ask questions about your data in plain English. Scout answers through a semantic model of your workspace's data, falling back to read-only SQL when the model can't express the question.

**Artifacts** — Responses can include rich artifacts: Recharts-powered charts, semantic stories, dashboards, and interactive visualizations.

**Sharing** — Share conversations and recipe runs through public links. Recipes and their runs are visible to everyone in the workspace.

**Recipes** — Save common workflows as reusable recipes with variables, so anyone on your team can re-run them without writing prompts from scratch.

## Documentation sections

- **[Getting started](getting-started/)** — Installation, setup, and your first conversation
- **[User guide](guide/)** — Querying data, understanding results, artifacts, sharing, and recipes
- **[Admin guide](admin/)** — Workspaces, members and roles, user accounts, and the knowledge base
- **[Deployment](deployment/)** — Docker, manual setup, and environment variable reference
- **[Reference](reference/)** — API endpoints, security model, and artifact types

## Design

**[Core design](reference/design.md)** — How tenants, workspaces, roles and permissions, invitations, threads, artifacts, recipes and multi-tenant workspaces behave.
