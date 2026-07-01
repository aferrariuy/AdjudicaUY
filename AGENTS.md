# AdjudicaUY — Agent Instructions

Government procurement viewer for Uruguay. FastAPI web app + scraper worker sharing a PostgreSQL database.

## Stack

- Python 3.13, FastAPI, SQLAlchemy 2.0, PostgreSQL 16, Alembic
- Frontend: Jinja2 templates + HTMX (no JS framework)
- Scraper: httpx + lxml, parses XML procurement reports from comprasestatales.gub.uy
- Worker: persistent scheduler (`schedule` lib), runs scraper once daily at 02:00 UTC
- Deploy: Docker Compose (app + worker + db), Dokploy in production

## Quick Commands

```bash
# Dev server
uvicorn app.main:app --reload

# Tests (in-memory SQLite — no DB needed)
pytest
pytest tests/scraper/test_xml_report.py          # single file
pytest -k "test_parse"                            # single test

# Lint/format (ruff replaces flake8+black+isort)
ruff check --fix
ruff format

# Type check
mypy

# Scraper
python -m scraper.main                            # one-shot scrape (today)
python -m scraper.scheduler                       # persistent daily scheduler

# Migrations
alembic upgrade head
alembic revision --autogenerate -m "description"

# Full stack (Postgres + app + worker)
docker-compose up --build
docker-compose up -d db                           # just the database
```

## Architecture

```
app/                  # FastAPI web app
  main.py             # App factory, uvicorn entry point
  config.py           # Pydantic Settings (env vars: DATABASE_URL, SOURCE_A_BASE_URL, BCU_API_URL)
  database.py         # SQLAlchemy engine + session factory (lazy init)
  models/             # ORM: Compra, Adjudicacion, Oferente (3 tables)
  routes/             # Thin route layer — only adjudications.py
  services/           # adjudication_service.py — all SQLAlchemy queries live here
  templates/          # Jinja2 HTML + HTMX partials

scraper/              # Scraper worker
  main.py             # run_scrape() — the single public entry point
  scheduler.py        # Daily scheduler (python -m scraper.scheduler)
  xml_report.py       # Fetch + parse government XML reports
  normalizer.py       # Currency conversion, row normalization
  bcu_client.py       # BCU SOAP client for exchange rates
  organism_lookup.py  # Static (id_inciso, id_ue) → organism name mapping
  ucc_lookup.py       # UCC codiguera fallback for organism resolution

tests/
  conftest.py         # In-memory SQLite engine, factories (make_adjudication, make_xml_compra)
  app/                # Route + model tests
  scraper/            # Scraper unit tests (parser, normalizer, lookups)
  e2e/                # Full pipeline smoke test (scrape → store → serve)
```

## Key Patterns

- **Routes are thin**: parse query params → call service → render template. No SQLAlchemy in routes.
- **Service layer**: `app/services/adjudication_service.py` owns all queries. Routes depend on it.
- **Idempotent scraper**: `ON CONFLICT DO NOTHING` on `compra.id_compra`. Re-runs are safe.
- **Organism resolution**: 2-step chain — `(id_inciso, id_ue)` lookup first, then UCC fallback, then `"Desconocido"` placeholder.
- **Date handling**: Scraper defaults to today. Routes default to current calendar year when no date params.

## Testing

- Tests use **in-memory SQLite**, not PostgreSQL. `conftest.py` sets `DATABASE_URL=sqlite:///:memory:` at import time.
- `pytest.ini` adds `.` to `pythonpath` — `app.*` and `scraper.*` imports work without editable install.
- `make_adjudication` factory supports both legacy field names (`winning_company`, `organism`) and new schema names (`nombre_comercial`, `compra_overrides`).
- E2E tests mock `fetch_xml_report` and `BcuClient` — no network calls, no Docker.
- Test markers: `slow`, `integration` (declared in `pytest.ini`, not yet widely used).

## Linting

- **Ruff** is the single tool for lint + format. Config in `pyproject.toml`.
- Per-file ignores: `B008` in `app/` (FastAPI `Depends()` pattern), `S320` in `scraper/` (lxml), `S101` in `tests/` (assert).
- **Pre-commit hooks**: ruff (lint + format) and mypy. Run `pre-commit install` once.

## Gotchas

- `.env` is gitignored. Copy `.env.example` → `.env` for local dev.
- `docker-compose.override.yml` provides a local PostgreSQL container with hardcoded dev credentials. Use it for standalone local dev.
- The worker container runs `entrypoint.sh` (alembic upgrade) before the scheduler. Migrations apply automatically on deploy.
- `conftest.py` mutates `os.environ` at import time. If you add a new `Settings` field, add it to `_TEST_ENV` or tests will fail.
- The scraper worker is a long-running process (not a cron job). Dokploy needs it running for its Scheduled Jobs feature.
- No CI workflows configured yet (no `.github/` directory).

## Project Skills

- `local-dev` — Set up and run AdjudicaUY locally with Python venv and Podman PostgreSQL. Covers venv creation, dependency install, Postgres container, migrations, and running the app/worker.

## Conventional Commits

The repo uses conventional commits: `feat(scope):`, `fix(scope):`, `test(scope):`, `style:`, `chore:`. Scopes seen: `scraper`, `db`, `scripts`, `test`, `normalizer`.
