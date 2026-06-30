# syntax=docker/dockerfile:1.7
#
# AdjudicaUY — multi-stage image for the FastAPI web app and the scraper
# worker. The same image is used for both ``app`` and ``worker`` services;
# ``docker-compose.yml`` overrides ``CMD`` for the worker.
#
# Stages:
#   * builder  — installs Python dependencies into a virtualenv
#   * tailwind — compiles ``app/static/css/style.css`` from Tailwind sources
#   * runtime  — copies the venv + compiled CSS + source, runs as non-root
#
# Python 3.13 to match the project's runtime (see ``.python-version`` and
# the scraper/type annotations in ``app/`` and ``scraper/``).

# ---------------------------------------------------------------------------
# builder
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS builder

WORKDIR /build

# Compilers and libpq headers are only needed here to build wheels for
# psycopg2-binary and lxml. They are NOT carried over to the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies into an isolated virtualenv. Copying this venv
# to the runtime stage avoids re-installing on every code change.
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade "pip==25.0.1" \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


# ---------------------------------------------------------------------------
# tailwind
# ---------------------------------------------------------------------------
# Compiles the Tailwind source bundle into ``static/css/style.css`` which
# the runtime stage copies into ``app/static/css/style.css``. We pin the
# major Node version so the image is reproducible.
FROM node:22-alpine AS tailwind

WORKDIR /src

# Install dependencies with pnpm using the committed lockfile. Corepack
# ships the correct pnpm version for the project, and ``--frozen-lockfile``
# guarantees a reproducible, audited dependency tree.
COPY package.json pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile

# Copy Tailwind source (config + CSS) and the templates so the content
# scanner can find every utility class in use. Without the templates,
# Tailwind purges ALL utilities and ships an empty bundle.
COPY tailwind.config.js ./
COPY static/ ./static/
COPY app/templates/ ./app/templates/
RUN pnpm exec tailwindcss -i static/src/input.css -o app/static/css/style.css --minify


# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

# libpq5 is the only runtime dependency of psycopg2-binary. We install it
# explicitly so the final image stays small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash --uid 1000 app

# Sensible runtime defaults. PYTHONUNBUFFERED keeps log output streaming
# (important for the scraper worker, which is short-lived).
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Virtualenv first (changes infrequently → good layer cache).
COPY --from=builder /opt/venv /opt/venv

# Compiled Tailwind bundle — a single file that includes the custom
# layer (corner decorations, masthead double border, dotted links,
# reduced-motion overrides) because ``input.css`` ``@import``s
# ``custom.css`` before emitting utilities.
COPY --from=tailwind /src/app/static/css/style.css ./app/static/css/style.css

# Application source. Owned by the unprivileged ``app`` user.
COPY --chown=app:app app/ ./app/
COPY --chown=app:app scraper/ ./scraper/
COPY --chown=app:app migrations/ ./migrations/
COPY --chown=app:app alembic.ini ./alembic.ini
COPY --chown=app:app scripts/entrypoint.sh ./scripts/entrypoint.sh
COPY --chown=app:app scripts/scrape_day_by_day.py ./scripts/scrape_day_by_day.py

USER app

EXPOSE 8000

# Entrypoint runs database migrations before the CMD.
# The ``worker`` service in ``docker-compose.yml`` overrides CMD
# with ``python -m scraper.main``; migrations still run first.
ENTRYPOINT ["bash", "scripts/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
