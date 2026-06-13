# syntax=docker/dockerfile:1.7
#
# AdjudicaUY — multi-stage image for the FastAPI web app and the scraper
# worker. The same image is used for both ``app`` and ``worker`` services;
# ``docker-compose.yml`` overrides ``CMD`` for the worker.
#
# Stages:
#   * builder — installs Python dependencies into a virtualenv
#   * runtime — copies the venv + source, runs as a non-root user
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

# Application source. Owned by the unprivileged ``app`` user.
COPY --chown=app:app app/ ./app/
COPY --chown=app:app scraper/ ./scraper/
COPY --chown=app:app migrations/ ./migrations/
COPY --chown=app:app alembic.ini ./alembic.ini
COPY --chown=app:app scripts/entrypoint.sh ./scripts/entrypoint.sh

USER app

EXPOSE 8000

# Entrypoint runs database migrations before the CMD.
# The ``worker`` service in ``docker-compose.yml`` overrides CMD
# with ``python -m scraper.main``; migrations still run first.
ENTRYPOINT ["bash", "scripts/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
