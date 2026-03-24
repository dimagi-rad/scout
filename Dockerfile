# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Set work directory
WORKDIR /app

# Copy dependency files first (cache layer — only re-installs when deps change)
COPY pyproject.toml uv.lock ./

# Install dependencies from lock file (cached unless pyproject.toml/uv.lock change)
RUN uv export --frozen --no-dev --no-emit-project > /tmp/requirements.txt \
    && uv pip install --system -r /tmp/requirements.txt

# Copy the rest of the project (code changes don't bust the dep cache above)
COPY . .

# Install the project itself (fast — deps already installed)
RUN uv pip install --system --no-deps -e .

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Default command
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
