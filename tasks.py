"""Invoke tasks for local development."""

from invoke import Context, task


@task
def dev(c: Context) -> None:
    """Start all dev servers (Django :8000, MCP :8100, Vite :5173) via honcho."""
    c.run("uv run honcho -f Procfile.dev start", pty=True)


@task
def deps(c: Context) -> None:
    """Start Docker dependencies: platform-db, redis, mcp-server."""
    c.run("docker compose up platform-db redis mcp-server", pty=True)


@task
def migrate(c: Context) -> None:
    """Run Django database migrations."""
    c.run("uv run python manage.py migrate", pty=True)


@task
def makemigrations(c: Context) -> None:
    """Create new Django migration files."""
    c.run("uv run python manage.py makemigrations", pty=True)


@task
def test(c: Context, path: str = "", k: str = "") -> None:
    """Run backend tests. Use -p for a file path, -k for a test name filter."""
    cmd = "uv run pytest"
    if path:
        cmd += f" {path}"
    if k:
        cmd += f" -k {k}"
    c.run(cmd, pty=True)


@task
def lint(c: Context) -> None:
    """Run ruff linter."""
    c.run("uv run ruff check .", pty=True)


@task
def fmt(c: Context) -> None:
    """Run ruff formatter."""
    c.run("uv run ruff format .", pty=True)


@task
def frontend_install(c: Context) -> None:
    """Install frontend dependencies with bun."""
    c.run("cd frontend && bun install", pty=True)


@task
def frontend_dev(c: Context) -> None:
    """Start the frontend dev server on :5173."""
    c.run("cd frontend && bun dev", pty=True)


@task
def frontend_build(c: Context) -> None:
    """Build the frontend for production (runs tsc first)."""
    c.run("cd frontend && bun run build", pty=True)


@task
def frontend_lint(c: Context) -> None:
    """Run frontend ESLint."""
    c.run("cd frontend && bun run lint", pty=True)


@task
def check(c: Context) -> None:
    """Run all linting and format checks (ruff lint, ruff format, frontend ESLint)."""
    c.run("uv run ruff check .", pty=True)
    c.run("uv run ruff format --check .", pty=True)
    c.run("cd frontend && bun run lint", pty=True)


@task
def docker_up(c: Context) -> None:
    """Start all services via Docker Compose (api :8000, frontend :3000, mcp :8100)."""
    c.run("docker compose up", pty=True)
