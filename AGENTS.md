# AI Agent Context

This file provides repository-specific context for coding agents.

## Recent Infrastructure Changes
- Docker Compose support has been added in [docker-compose.yml](docker-compose.yml).
- Compose now defines `opentakserver`, `postgres`, and `rabbitmq` services.
- OpenTAKServer container receives explicit DB and RabbitMQ settings via environment variables.
- Persistent named volumes were added for app data, Postgres, and RabbitMQ.

## Container Compatibility Notes
- [Dockerfile](Dockerfile) now uses Python 3.12 (not 3.13) due to gevent fork-hook assertions observed with Python 3.13.
- `curl` is installed in the image because container healthcheck uses HTTP probe against `/api/health`.
- Flask app path in image build step was updated to Python 3.12 site-packages.

## Expected Compose Behavior
- `docker compose config` should validate without errors.
- `postgres` and `rabbitmq` should become healthy before app startup (`depends_on` with health conditions).
- OpenTAKServer healthcheck endpoint is `/api/health`.

## Agent Guidance
When changing deployment or startup behavior:
1. Keep [docker-compose.yml](docker-compose.yml), [Dockerfile](Dockerfile), and [README.md](README.md) in sync.
2. Prefer minimal, backward-compatible edits to runtime environment variables.
3. Re-run configuration validation (`docker compose config`) after changes.
4. If startup regressions appear, check OpenTAKServer logs for Python/gevent compatibility first.
