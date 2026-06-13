#!/usr/bin/env bash
set -euo pipefail

echo "==> DEBUG: Checking environment variables:"
echo "DATABASE_URL=${DATABASE_URL:-<NOT SET>}"
echo "POSTGRES_USER=${POSTGRES_USER:-<NOT SET>}"
echo "POSTGRES_PASSWORD is $( [ -n "${POSTGRES_PASSWORD:-}" ] && echo SET || echo NOT SET )"
echo "SOURCE_A_BASE_URL=${SOURCE_A_BASE_URL:-<NOT SET>}"
echo "SOURCE_B_BASE_URL=${SOURCE_B_BASE_URL:-<NOT SET>}"
echo "BCU_API_URL=${BCU_API_URL:-<NOT SET>}"
echo "==> Running database migrations..."
alembic upgrade head

echo "==> Starting uvicorn..."
exec "$@"
