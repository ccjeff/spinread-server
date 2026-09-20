#!/usr/bin/env bash
# Bring up local dev infrastructure and run migrations (idempotent).
set -euo pipefail
cd "$(dirname "$0")/.."

# Docker Desktop on macOS does not put docker on PATH for non-login shells.
if ! command -v docker >/dev/null 2>&1; then
  export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
fi

docker compose up -d

echo "waiting for postgres..."
until docker compose exec -T postgres pg_isready -U spinread -d spinread >/dev/null 2>&1; do
  sleep 1
done

echo "waiting for minio..."
until curl -sf http://localhost:9000/minio/health/live >/dev/null 2>&1; do
  sleep 1
done

PY="${PYTHON:-.venv/bin/python}"
"$PY" -m alembic upgrade head
echo "dev stack is up: postgres :5432, minio :9000 (console :9001)"
