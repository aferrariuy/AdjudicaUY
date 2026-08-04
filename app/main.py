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
from urllib.parse import quote

from fastapi import Depends, FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware

from app.config import get_settings
from app.database import get_db, get_engine
from app.formatting import format_count, format_percent, format_uyu
from app.routes.adjudications import router as adjudications_router
from app.services.adjudication_service import all_companies, all_organisms

logger = logging.getLogger(__name__)

# Resolve paths once at import time so the app factory is cheap to call.
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
# Static assets live next to the package (``app/static``) rather than
# under ``app/templates/static`` so the Tailwind build output and any
# other compiled bundles have a stable, framework-agnostic home.
STATIC_DIR = Path(__file__).resolve().parent / "static"


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

    # GZip compression — compresses responses larger than 500 bytes.
    # Must be added first so it wraps the response after other
    # middlewares set headers.
    app.add_middleware(GZipMiddleware, minimum_size=500)

    # Security headers middleware — adds X-Content-Type-Options and
    # X-Frame-Options on every response. HSTS is only added when not
    # in debug mode so local development is not affected.
    settings = get_settings()

    @app.middleware("http")
    async def add_security_headers(request, call_next):  # noqa: ANN001, ANN202
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if not settings.debug:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    # Cache-Control for static assets — aggressive caching since files
    # are content-hashed. Only applies to /static/ prefix.
    @app.middleware("http")
    async def add_cache_control(request, call_next):  # noqa: ANN001, ANN202
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Lightweight liveness probe for container orchestrators."""

        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Crawler directives: robots.txt + sitemap.xml
    # ------------------------------------------------------------------

    @app.get("/robots.txt", include_in_schema=False)
    async def robots_txt() -> Response:
        """Serve a robots.txt allowing all crawlers and referencing the sitemap."""

        body = f"User-agent: *\nAllow: /\nSitemap: {settings.site_url}/sitemap.xml\n"
        return Response(content=body, media_type="text/plain")

    @app.get("/sitemap.xml", include_in_schema=False)
    async def sitemap_xml(db=Depends(get_db)) -> Response:  # noqa: ANN001
        """Serve a sitemap.xml listing index, organisms, and companies."""

        organisms = all_organisms(db)
        companies = all_companies(db)
        urls = [f"{settings.site_url}/"]
        for name in organisms:
            encoded = quote(name, safe="")
            urls.append(f"{settings.site_url}/organism/{encoded}")
        for company_type, company_number in companies:
            encoded_type = quote(company_type, safe="")
            encoded_number = quote(company_number, safe="")
            urls.append(f"{settings.site_url}/company/{encoded_type}/{encoded_number}")

        url_entries = "\n".join(f"  <url><loc>{url}</loc></url>" for url in urls)
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{url_entries}\n"
            "</urlset>"
        )
        return Response(content=body, media_type="application/xml")

    app.include_router(adjudications_router)

    return app


# Module-level instance for ``uvicorn app.main:app``.
app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)  # noqa: S104 — required for Docker
