# Docker Compose Notes

## Scope
This document describes the Docker Compose support added to this repository and the runtime assumptions for local/container development.

## What Was Added
- Added a Compose stack file: [docker-compose.yml](../docker-compose.yml)
- Added a quick-start section in [README.md](../README.md)

## Compose Services
The stack now starts 3 services:
1. OpenTAKServer application container (`opentakserver`)
2. PostgreSQL database (`postgres`)
3. RabbitMQ with management UI (`rabbitmq`)

## Key Runtime Settings
OpenTAKServer service uses these environment values in Compose:
- `OTS_LISTENER_ADDRESS=0.0.0.0`
- `OTS_RABBITMQ_SERVER_ADDRESS=rabbitmq`
- `OTS_RABBITMQ_USERNAME=guest`
- `OTS_RABBITMQ_PASSWORD=guest`
- `SQLALCHEMY_DATABASE_URI=postgresql+psycopg://ots:ots@postgres/ots`
- `OTS_MEDIAMTX_ENABLE=False` (disabled for simpler baseline startup)
- `OTS_DATA_FOLDER=/app/ots`

## Persistent Volumes
- `ots_data` for OpenTAKServer data
- `postgres_data` for PostgreSQL data
- `rabbitmq_data` for RabbitMQ data

## Dockerfile Adjustments (for Compose reliability)
The Docker image was updated to improve compatibility and health checks:
- Base image changed from Python 3.13 to Python 3.12
- Added `curl` package because healthcheck depends on it
- Updated Flask app path from Python 3.13 site-packages to Python 3.12 site-packages

## Why Python 3.12
During Compose startup, gevent emitted an assertion tied to Python 3.13 in fork hooks. Pinning the image to Python 3.12 avoids this incompatibility in the current dependency set.

## Validation Performed
- `docker compose config` parses correctly
- Stack services are created with health checks and dependency ordering

## Operational Commands
- Start: `docker compose up -d --build`
- Logs: `docker compose logs -f opentakserver postgres rabbitmq`
- Stop: `docker compose down`
- Reset data volumes: `docker compose down -v`
