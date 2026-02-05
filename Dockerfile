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

# Copy dependency files
COPY pyproject.toml .

# Install dependencies (production only, no dev dependencies)
RUN uv pip install --system -e .

# Copy project
COPY . .

# Install Playwright browsers for artifact export
RUN python -m playwright install --with-deps chromium

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Default command
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
