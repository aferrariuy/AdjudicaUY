# AdjudicaUY — Agent Instructions

Government procurement viewer for Uruguay: a FastAPI web app (Jinja2 + HTMX + Tailwind) and a scraper worker sharing one PostgreSQL database. The worker ingests daily XML procurement reports from comprasestatales.gub.uy; the web app lets citizens search and visualize adjudications.

This file is the source of truth for project conventions, architecture decisions, and operational gotchas. **Update it whenever reality changes** — stale docs mislead both humans and agents.

## Stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.13, FastAPI, uvicorn |
| Persistence | PostgreSQL 16, SQLAlchemy 2.0, Alembic |
| Frontend | Jinja2 templates + HTMX (no JS framework), Tailwind CSS (compiled, committed output is gitignored) |
| Scraper | httpx + lxml (XML reports), SOAP client for BCU exchange rates |
| Worker | Persistent scheduler (`schedule` lib), daily at 02:00 UTC by default |
| Quality | ruff, mypy, bandit, pip-audit, pre-commit, GitHub Actions CI |
| Deploy | Docker Compose (app + worker + db) on Dokploy |

## Quick Commands

```bash
# Dev server
uvicorn app.main:app --reload

# Tests (in-memory SQLite — no DB needed)
pytest
pytest tests/scraper/test_xml_report.py          # single file
pytest -k "test_parse"                            # single test

# Lint/format/type-check
ruff check --fix
ruff format
mypy

# Scraper
python -m scraper.main                            # one-shot scrape (today)
python -m scraper.scheduler                       # persistent daily scheduler
python scripts/scrape_day_by_day.py               # soft-failure backfill, day by day

# Tailwind CSS (output app/static/css/style.css is gitignored)
pnpm build:css                                    # one-shot build
pnpm watch:css                                    # watch mode

# Migrations
alembic upgrade head
alembic revision --autogenerate -m "description"

# Full stack (Postgres + app + worker)
docker-compose up --build
docker-compose up -d db                           # just the database

# Hooks
pre-commit install                                # once per checkout
pre-commit run --all-files                        # validate everything
```

## Architecture

```
app/                  # FastAPI web app
  main.py             # create_app() factory, lifespan (env validation + engine warm-up),
                      # middleware (gzip, security headers, static cache), /healthz,
                      # /robots.txt, /sitemap.xml
  config.py           # Pydantic Settings; URL validators enforce HTTPS + host allowlist
  database.py         # Lazy engine + session factory (pool_pre_ping), get_db dependency
  formatting.py       # es-UY number formatting as Jinja filters (no system locale needed)
  models/             # ORM: Compra, Adjudicacion, Oferente (3 tables)
  routes/             # HTTP layer — currently a single adjudications.py (9 routes)
  services/           # adjudication_service.py (all SQLAlchemy queries) + query_cache.py
  templates/          # pages/ (5) + partials/ (9): Jinja2 + HTMX fragments

scraper/              # Worker pipeline: fetch → parse → enrich → normalize → persist
  main.py             # run_scrape() — orchestration, batched flush, per-day isolation
  scheduler.py        # Daily scheduler loop with SIGTERM/SIGINT graceful shutdown
  xml_report.py       # Fetch + parse government XML (lxml), partial recovery
  normalizer.py       # Row normalization + currency conversion (amount_uyu)
  bcu_client.py       # BCU SOAP client: per-(code, date) cache, 7-day lookback, retry
  persistence.py      # Idempotent inserts: ON CONFLICT DO NOTHING on all 3 tables
  retry.py            # Shared retry_with_backoff (1s/3s/9s + jitter, transport-agnostic)
  organism_lookup.py  # Static (id_inciso, id_ue) → organism name mapping
  ucc_lookup.py       # UCC codiguera fallback for organism resolution

scripts/
  scrape_day_by_day.py    # Soft-failure day-by-day backfill (own try/except around inserts)
  backfill_inciso_ue.py   # One-off data repair for organism columns
  entrypoint.sh           # Container entrypoint: alembic upgrade (5 retries) then exec CMD

tests/
  conftest.py         # In-memory SQLite env setup (_TEST_ENV), factories
  app/                # Routes, service, model, config, query-cache tests
  scraper/            # Parser, normalizer, lookups, retry, BCU client, persistence
  e2e/                # Full pipeline smoke test (scrape → store → serve, all mocked)
  + root-level suites: SEO, security headers, cache control, robots/sitemap, migrations
```

## HTTP Surface

All routes live in `app/routes/adjudications.py` and are excluded from OpenAPI schema.

| Path | Handler | Purpose |
|---|---|---|
| `GET /` | `index` | Full dashboard page: filters, listing, KPI, trend, concentration, rankings |
| `GET /adjudications` | `adjudications_partial` | HTMX partial for the results container; `partial=table` skips aggregates (pagination) |
| `GET /adjudications/export` | `_stream_csv_response` | CSV export of the filtered listing |
| `GET /organism/{name}` | `organism_detail` | Full organism profile page |
| `GET /organism/{name}/partial` | `organism_detail_partial` | HTMX-swappable organism profile body |
| `GET /company/{tipo_doc_prov}/{nro_doc_prov}` | `company_detail` | Full company profile page (summary, win rate, competitors, rankings) |
| `GET /company/{...}/export` | `export_company_adjudications` | CSV export scoped to one company |
| `GET /company/{...}/partial` | `company_detail_partial` | HTMX company profile body; `partial=table` for the table only |
| `GET /about` | `about` | Informational page |

Plus in `app/main.py`: `/healthz` (compose healthcheck), `/robots.txt`, `/sitemap.xml` (index + organisms + companies).

## Data Model

| Table | Natural key / uniqueness | Notes |
|---|---|---|
| `compra` | `id_compra` unique | One per `<compra>` XML element; nullable XML attributes tolerate schema drift; `organismo` nullable (lookup may miss); indexes on filter/ordering columns (`fecha_pub_adj`, `id_inciso`, `id_ue`, `id_tipocompra`, `id_ucc`) |
| `adjudicacion` | `(compra_id, nombre_comercial, desc_articulo)` | One line item per award; `amount_uyu` nullable (non-convertible currencies); `ON DELETE CASCADE` from compra |
| `oferente` | `(compra_id, nombre_comercial)` | One bidder per purchase; flat row, raw `id_moneda` only (no normalized amount) |

Child rows link via `compra_id` FK with `ON DELETE CASCADE`. The unique constraints make re-runs idempotent at the database level.

## Key Patterns

- **Layered web app**: routes parse/validate params → call service → shape view-model → render. Services own every SQLAlchemy query; routes never touch the ORM. Templates read display DTOs (`AdjudicationRow` frozen dataclass), never ORM models.
- **App factory** (`create_app()`): no import-time side effects; templates on `app.state` (tests can swap the Jinja environment); `lifespan` fails fast on bad config and warms the engine.
- **Lazy engine/session**: `get_engine()`/`get_session_factory()` build once per process; `pool_pre_ping` guards stale connections; `expire_on_commit=False`; `get_db` yields and closes a request-scoped session.
- **Idempotent ingestion**: `ON CONFLICT DO NOTHING` on the natural key of all three tables (compra first, then resolve PKs, then children). `run_scrape` batches with `flush_size` (1000) / `flush_interval` (7 days) thresholds and drains the buffer in `finally`.
- **Failure isolation**: per-day fetch failures skip the day; per-compra normalization failures log and continue; malformed XML blocks are skipped but the parent compra is still yielded. DB errors propagate (fail-hard) in the worker; the day-by-day script soft-fails.
- **Shared retry**: `retry_with_backoff` (1s/3s/9s + uniform jitter, configurable retryable tuple) is transport-agnostic; the BCU wrapper re-raises `BcuError`.
- **BCU rate cache**: `BcuClient` caches `(bcu_code, date)` including confirmed-empty results (weekends/holidays) and walks back up to 7 days for the first available rate.
- **Organism resolution chain**: `(id_inciso, id_ue)` static map → `id_ucc` codiguera → `"Desconocido (x-y)"` fallback. Never raises — the pipeline must always produce a row.
- **Aggregate cache** (`query_cache.py`): process-local TTL/LRU over a **whitelist** of aggregate names; normalized JSON keys (limit only for limit-aware aggregates); `RLock`; TTL is 0 (disabled) or 300–900s; LRU eviction at `cache_max_entries`. Cache is TTL-only — no invalidation on scrape.
- **HTMX partials**: full-page routes render the same data as their `/partial` counterparts; `partial=table` skips the expensive aggregates for pagination requests; `HX-Request` is logged for tracing.
- **Validation**: `validate_date_params` raises `DateValidationError` (unparseable, reversed range, >5 years); full-page routes render 422 with the user's input preserved; out-of-bounds page numbers redirect (302) to the last valid page instead of 4xx.
- **SEO**: per-route `_build_seo_context` (meta + OG), sitemap.xml, robots.txt, canonical paths.
- **es-UY formatting**: `app/formatting.py` implements thousands/decimal/percent formatting as Jinja filters because the deploy image lacks the `es_UY` locale.
- **Config safety**: Pydantic Settings validators enforce HTTPS and a host allowlist (`comprasestatales.gub.uy`, `cotizaciones.bcu.gub.uy`); test mode (via `PYTEST_CURRENT_TEST`) permits `example.test` hosts; credentials are stripped from log lines.

## Testing

- **In-memory SQLite**: `tests/conftest.py` mutates `os.environ` at import time (`_TEST_ENV`). No DB or network needed.
- **Factories**: `make_adjudication` (accepts both legacy kwargs like `winning_company`/`organism` and new schema names/`compra_overrides`), `make_oferente`, `make_xml_compra`.
- **E2E**: `tests/e2e/test_scrape_to_serve.py` mocks `fetch_xml_report` and `BcuClient` — full pipeline without network.
- **Markers**: `slow`, `integration` declared in `pytest.ini`.
- Keep the suite green locally (`pytest`) and in CI — same command, same expectations.

## Quality Gates

- **CI** (`.github/workflows/ci.yml`, push/PR to `master`): Lint (ruff check + ruff format --check + mypy), Test (pytest), Security (bandit + pip-audit). Python 3.13 with pip cache.
- **Pre-commit**: ruff `--fix`, ruff-format, mypy.
- **Ruff** (`pyproject.toml`): line-length 88, py313 target; rules `E,F,I,UP,B,S,A,C4,SIM,TCH`; per-file ignores: `B008` in `app/` (FastAPI `Depends()`), `S320` in `scraper/` (lxml), `S101` in `tests/`, `E501` for XML fixture test files.
- **Bandit**: skips `B104` (uvicorn binds `0.0.0.0` in the container) and `B311` (retry jitter) — both deliberate and documented in `pyproject.toml`.

## Deployment

- **Compose** (`docker-compose.yml`): `db` (postgres:16-alpine, healthcheck, internal network only), `app` (uvicorn) and `worker` (`python -m scraper.scheduler`), both hardened with `read_only`, `cap_drop: ALL`, `no-new-privileges`, tmpfs `/tmp`. Env comes from `.env`; `dokploy-network` is external.
- **Dockerfile** is multi-stage: `builder` (Python venv), `tailwind` (pnpm + Tailwind compile with template content scan), `runtime` (non-root uid 1000, libpq5 only).
- **Entrypoint** (`scripts/entrypoint.sh`): runs `alembic upgrade head` with 5 retries (5s apart) then `exec "$@"`; migrations are serialized via PostgreSQL advisory locks in `migrations/env.py`.
- **Worker schedule**: daily at `SCRAPE_HOUR:SCRAPE_MINUTE` UTC (default 02:00 = 23:00 Montevideo). The worker is a long-running container — Dokploy's Scheduled Jobs feature needs it running.

## Conventions

- **Conventional commits**. Scopes seen: `scraper`, `db`, `scripts`, `test`, `normalizer`, `perf`, `ci`, `security`, `style`, `chore`, `fix`.
- No AI attribution ("Co-Authored-By") in commits.
- Default branch is `master`; PRs target `master` (CI triggers on those events).
- Generated artifacts (code, UI copy, docs) default to English. Agent replies to the user follow the user's language.

## Gotchas

- `.env` is gitignored — copy `.env.example` → `.env`. `docker-compose.override.yml` is also gitignored (local-only dev Postgres with hardcoded credentials).
- `conftest.py` mutates `os.environ` at import time: **every new `Settings` field must be added to `_TEST_ENV`** or the test suite breaks.
- Settings URL validators reject non-HTTPS and unknown hosts; tests rely on `PYTEST_CURRENT_TEST` to allow `example.test` hosts. `ALLOW_HTTP_SOURCE_URL=true` is the explicit escape hatch.
- Tailwind output (`app/static/css/style.css`) is **gitignored build output** — run `pnpm build:css` after template/CSS changes. `fonts.css` is committed.
- Dashboard aggregate cache is TTL-only: data from the daily 02:00 scrape can take up to `cache_ttl_seconds` to appear.
- Migration revision IDs must fit `alembic_version` varchar(32) — keep revision IDs short.
- The worker has **no catch-up**: if it was down at scrape time, that day waits until the next run.
- `parse_xml_report` requires bytes (the upstream payload declares ISO-8859-1); pass bytes, not str.
- URL-decoding: routes unquote path segments explicitly so doubled encoding round-trips cleanly.

## Project Skills

Project skills are registered in `.atl/skill-registry.md` (cache: `.atl/.skill-registry.cache.json`). Load matching skills before task-specific work.

- `local-dev` — Set up and run AdjudicaUY locally with Python venv and Podman PostgreSQL. Covers venv creation, dependency install, Postgres container, migrations, and running the app/worker.

<!-- CODEGRAPH_START -->
## CodeGraph

In repositories indexed by CodeGraph (a `.codegraph/` directory exists at the repo root), reach for it BEFORE grep/find or reading files when you need to understand or locate code:

- **MCP tool** (when available): `codegraph_explore` answers most code questions in one call — the relevant symbols' verbatim source plus the call paths between them, including dynamic-dispatch hops grep can't follow. Name a file or symbol in the query to read its current line-numbered source. If it's listed but deferred, load it by name via tool search.
- **Shell** (always works): `codegraph explore "<symbol names or question>"` prints the same output.

If there is no `.codegraph/` directory, skip CodeGraph entirely — indexing is the user's decision.
<!-- CODEGRAPH_END -->
