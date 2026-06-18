"""FastAPI application factory and entry point.

Run locally with::

    uvicorn app.main:app --reload

The module exposes :data:`app` (a module-level ``FastAPI`` instance built
by :func:`create_app`) so the ``uvicorn app.main:app`` form works
without further configuration. The factory is also exported for tests
and for the ``Dockerfile``'s ``uvicorn`` command.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.database import get_engine
from app.formatting import format_count, format_percent, format_uyu
from app.routes.adjudications import router as adjudications_router

logger = logging.getLogger(__name__)

# Resolve paths once at import time so the app factory is cheap to call.
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = TEMPLATES_DIR / "static"


def _validate_environment() -> None:
    """Fail fast at startup if the environment is not configured.

    Settings come from environment variables; the only required ones for
    the web app are the database URL and the source URLs (the BCU URL
    is only used by the worker, but we still require it to keep the
    scraper and web deployable from the same image). Calling
    :func:`app.config.get_settings` instantiates the ``BaseSettings``
    object — Pydantic raises a clear ``ValidationError`` if any field
    is missing or unparseable.
    """

    settings = get_settings()
    logger.info(
        "Environment validated: database_url=%s source_a=%s bcu_api=%s",
        settings.database_url.split("@", 1)[-1],  # strip credentials
        settings.source_a_base_url,
        settings.bcu_api_url,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Validate configuration and warm up the DB engine on startup.

    Anything that needs to run *before* the first request lands belongs
    here. We deliberately do NOT open a session per request up-front —
    SQLAlchemy's pool lazily connects on first use, and ``pool_pre_ping``
    (configured in ``app.database``) protects against stale connections.
    """

    _validate_environment()
    # Touch the engine so any startup-time configuration error surfaces
    # now rather than on the first request.
    get_engine()
    logger.info("Application startup complete")
    yield
    logger.info("Application shutdown")


def create_app() -> FastAPI:
    """Build and return a fully-configured :class:`FastAPI` application.

    Keeping the construction inside a factory (rather than at module
    import time) makes the app easy to instantiate in tests with a
    temporary database, and avoids importing the routes when the
    module is loaded for tooling (e.g. ``python -c "import app.main"``).
    """

    app = FastAPI(
        title="AdjudicaUY",
        description="Visor de adjudicaciones del Estado uruguayo.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Templates are attached to ``app.state`` so the route handlers can
    # resolve them via ``request.app.state.templates``. This indirection
    # lets tests swap in a different Jinja2 environment.
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # Locale-aware number formatters. The deploy image does not ship the
    # ``es_UY`` system locale, so we wrap the pure-Python helpers from
    # :mod:`app.formatting` as Jinja filters. Templates use them like
    # ``{{ value | uyu }}`` / ``{{ value | count_uy }}`` /
    # ``{{ value | pct_uy }}``; the underlying strings match the spec
    # scenarios for total / count / percentage formatting.
    app.state.templates.env.filters["uyu"] = format_uyu
    app.state.templates.env.filters["count_uy"] = format_count
    app.state.templates.env.filters["pct_uy"] = format_percent

    # Optional static dir — created on demand by the first deployer.
    # Mounting a missing directory crashes uvicorn, so we only mount if
    # the path exists.
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(adjudications_router)

    return app


# Module-level instance for ``uvicorn app.main:app``.
app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)  # noqa: S104 — required for Docker
