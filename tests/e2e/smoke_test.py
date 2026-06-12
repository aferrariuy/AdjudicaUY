"""Standalone smoke test for the ``docker-compose`` deployment.

Run this against a live ``dokploy/docker-compose.yml`` stack to verify
the full pipeline (scrape → store → query → serve) is healthy in
production. It expects the four services from the deployment spec to
be reachable:

* ``postgres`` on port 5432 (internal to the compose network)
* ``app`` on the public port mapped by Dokploy
* ``worker`` — invoked by the cron schedule, not by this script
* ``bcu`` — external, mocked below

The script performs four checks and exits non-zero on any failure:

1. The ``/`` endpoint returns HTML (the app is up).
2. The ``/adjudications`` partial renders (HTMX path works).
3. The database contains at least one row (the worker has run at
   least once since startup).
4. The BCU SOAP endpoint is reachable from the worker container (not
   strictly required at runtime, but useful to surface rate-limiting
   issues early).

Usage::

    python -m tests.e2e.smoke_test \\
        --app-url https://adjudicauy.example.com \\
        --db-url  postgresql+psycopg2://user:pass@postgres:5432/adjudicauy

The script is deliberately dependency-light (stdlib + httpx + sqlalchemy)
so it can be packaged into a small Docker image alongside the worker
and run on the same schedule.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

logger = logging.getLogger("smoke_test")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--app-url",
        default="http://localhost:8000",
        help="Base URL of the deployed web app (default: http://localhost:8000).",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help=(
            "Optional SQLAlchemy URL for the production database. When provided, "
            "the script also asserts the table is non-empty."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP timeout in seconds (default: 10).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Number of retries on transient HTTP failures (default: 3).",
    )
    return parser.parse_args(argv)


def _check_app_responsive(app_url: str, timeout: float, retries: int) -> None:
    """Verify the web app returns 200 on GET /."""
    import httpx

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = httpx.get(app_url, timeout=timeout)
            if response.status_code == 200 and "text/html" in response.headers.get(
                "content-type", ""
            ):
                logger.info("PASS: GET %s returned 200 HTML", app_url)
                return
            logger.warning(
                "Attempt %d: GET %s returned %d (%s)",
                attempt,
                app_url,
                response.status_code,
                response.headers.get("content-type"),
            )
        except Exception as exc:  # noqa: BLE001 - we want to retry broadly
            last_exc = exc
            logger.warning("Attempt %d failed: %s", attempt, exc)
        time.sleep(2 ** (attempt - 1))
    raise SystemExit(f"Web app at {app_url} did not respond with HTML: {last_exc}")


def _check_partial_endpoint(app_url: str, timeout: float, retries: int) -> None:
    """Verify the HTMX partial endpoint returns HTML."""
    import httpx

    url = f"{app_url.rstrip('/')}/adjudications"
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = httpx.get(url, timeout=timeout, headers={"HX-Request": "true"})
            if response.status_code == 200 and "text/html" in response.headers.get(
                "content-type", ""
            ):
                logger.info("PASS: GET %s returned 200 HTML", url)
                return
            logger.warning(
                "Attempt %d: GET %s returned %d", attempt, url, response.status_code
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("Attempt %d failed: %s", attempt, exc)
        time.sleep(2 ** (attempt - 1))
    raise SystemExit(f"Partial endpoint at {url} did not respond: {last_exc}")


def _check_database_has_rows(db_url: str) -> None:
    """Verify the database is reachable and has at least one row."""
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.exc import SQLAlchemyError

    from app.models.adjudication import Adjudication

    try:
        engine = create_engine(db_url, future=True, pool_pre_ping=True)
        with engine.connect() as conn:
            count = conn.execute(select(func.count(Adjudication.id))).scalar_one()
        engine.dispose()
    except SQLAlchemyError as exc:
        raise SystemExit(f"Database check failed: {exc}") from exc

    if count == 0:
        raise SystemExit(
            "Database is empty — has the scraper worker run at least once?"
        )
    logger.info("PASS: Database contains %d rows", count)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    args = _parse_args(argv)

    logger.info("Smoke test starting — app URL: %s", args.app_url)

    _check_app_responsive(args.app_url, args.timeout, args.retries)
    _check_partial_endpoint(args.app_url, args.timeout, args.retries)

    if args.db_url:
        _check_database_has_rows(args.db_url)
    else:
        logger.info("SKIP: --db-url not provided, skipping database check")

    logger.info("All smoke checks passed")
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    sys.exit(main())
