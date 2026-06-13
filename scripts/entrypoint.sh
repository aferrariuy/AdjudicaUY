#!/usr/bin/env bash
set -euo pipefail

# Run database migrations before starting the app.
# Fails fast if DATABASE_URL or other required env vars are missing.
echo "==> Running database migrations..."
alembic upgrade head

echo "==> Starting uvicorn..."
exec "$@"
