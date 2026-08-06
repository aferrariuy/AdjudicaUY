#!/usr/bin/env bash
set -euo pipefail

echo "==> Running database migrations..."
# The app and worker containers both run ``alembic upgrade head`` on
# startup. When a deploy recreates both at once they race transiently
# (PostgreSQL advisory locks in migrations/env.py serialize the actual
# DDL), so retry briefly instead of crashing the container on the first
# failure. Migrations are atomic — a failed run leaves the schema
# unchanged and the retry converges.
migrated=0
for attempt in 1 2 3 4 5; do
    if alembic upgrade head; then
        migrated=1
        break
    fi
    echo "==> alembic upgrade attempt $attempt failed; retrying in 5s" >&2
    sleep 5
done

if [ "$migrated" -ne 1 ]; then
    echo "==> alembic upgrade failed after 5 attempts" >&2
    exit 1
fi

echo "==> Starting uvicorn..."
exec "$@"
