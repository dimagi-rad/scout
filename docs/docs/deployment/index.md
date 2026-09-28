# Deployment

Scout can be deployed using Docker Compose or manually with uvicorn and a frontend build.

## Options

- **[Docker](docker.md)** -- local Compose stack with PostgreSQL and Cube. Start the background worker separately; the checked-in development configuration is not production-hardened.
- **[Manual](manual.md)** -- run the API, MCP server, background worker, and frontend behind a reverse proxy, with a separately managed Cube runtime and validator.
- **[Configuration](configuration.md)** -- environment variable reference for all deployment options.
