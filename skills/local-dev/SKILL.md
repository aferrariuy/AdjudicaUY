---
name: local-dev
description: "Trigger: run locally, local dev, setup environment, podman postgres, virtualenv setup. Set up and run AdjudicaUY locally with Python venv and Podman PostgreSQL."
license: Apache-2.0
metadata:
  author: "AdjudicaUY"
  version: "1.0"
---

## Activation Contract

Use this skill when:
- Setting up the project for the first time on a new machine
- Running the app or worker locally outside Docker/Podman containers
- Troubleshooting local dev environment issues
- Starting/stopping the local PostgreSQL via Podman

## Hard Rules

- ALWAYS use `podman` (not `docker`) for the local PostgreSQL container.
- ALWAYS activate the Python venv before running any Python command.
- The `.env` file is required — copy from `.env.example` if missing.
- NEVER commit `.env` or `.venv/` to git.
- Postgres binds to `127.0.0.1:5432` only — never expose to the network.

## Decision Gates

| Situation | Action |
|-----------|--------|
| No `.env` file | `cp .env.example .env` then edit POSTGRES_PASSWORD |
| No `.venv/` | Create venv, install both requirement files |
| `.venv/` exists | Activate it, reinstall deps (safe to re-run) |
| Postgres not running | Start Podman container first |
| Port 5432 occupied | Kill existing process or change port in `.env` |

## Execution Steps

### 1. Setup virtual environment

```bash
# Check if venv exists, create only if missing
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate

# Install dependencies (always safe to re-run)
pip install --upgrade pip
pip install -r requirements.txt -r dev-requirements.txt

# Install pre-commit hooks
pre-commit install
```

### 2. Start PostgreSQL via Podman

```bash
# Start Postgres (detached, auto-restart, health check)
podman run -d \
  --name adjudicauy-postgres \
  --restart unless-stopped \
  -e POSTGRES_USER=adjudicauy \
  -e POSTGRES_PASSWORD=adjudicauy_dev \
  -e POSTGRES_DB=adjudicauy \
  -p 127.0.0.1:5432:5432 \
  -v postgres_data:/var/lib/postgresql/data \
  postgres:16-alpine

# Verify it's healthy (wait for "accepting connections")
podman logs -f adjudicauy-postgres
```

### 3. Configure environment

Edit `.env` and set:
```
DATABASE_URL=postgresql+psycopg2://adjudicauy:adjudicauy_dev@localhost:5432/adjudicauy
POSTGRES_HOST=localhost
```

### 4. Run migrations

```bash
alembic upgrade head
```

### 5. Run the app

```bash
uvicorn app.main:app --reload
# → http://localhost:8000
```

### 6. Run the worker (optional, separate terminal)

```bash
source .venv/bin/activate
python -m scraper.scheduler
```

### 7. Stop / restart

```bash
# Stop Postgres
podman stop adjudicauy-postgres

# Remove Postgres (data persists in volume)
podman rm adjudicauy-postgres

# Full cleanup (destroys data)
podman volume rm postgres_data
```

## Quick reference

| Command | Purpose |
|---------|---------|
| `source .venv/bin/activate` | Activate Python env |
| `podman start adjudicauy-postgres` | Start existing container |
| `podman stop adjudicauy-postgres` | Stop container |
| `podman ps` | Check running containers |
| `alembic upgrade head` | Apply pending migrations |
| `uvicorn app.main:app --reload` | Dev server with hot reload |
| `pytest` | Run test suite |
| `ruff check --fix && ruff format` | Lint and format |

## Output Contract

When executing this skill, return:
- Confirmation of each setup step completed
- The running service URLs (app at :8000, Postgres at :5432)
- Any errors encountered with resolution steps
