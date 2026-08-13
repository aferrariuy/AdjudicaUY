"""Long-running scheduler for the scraper worker.

Runs inside the Dokploy worker container as a persistent process. The
scheduler executes :func:`scraper.main.run_scrape` once daily at a
configurable time (default 02:00 UTC = 23:00 Montevideo).

Schedule configuration is tolerant by design: a missing, non-numeric,
or out-of-range ``SCRAPE_HOUR`` / ``SCRAPE_MINUTE`` falls back to the
default (02:00) with one logged error instead of crashing the worker.
The daily job is registered with an explicit UTC timezone.

Configuration (environment variables):
    SCRAPE_HOUR   — Hour of day to run (0-23, default: 2)
    SCRAPE_MINUTE — Minute of the hour (0-59, default: 0)

Usage::

    python -m scraper.scheduler
"""

from __future__ import annotations

import logging
import os
import signal
import time

import schedule

from scraper.main import _configure_logging, run_scrape

logger = logging.getLogger(__name__)

# Graceful shutdown flag — set by SIGTERM/SIGINT handlers so the main
# loop can exit cleanly when Docker stops the container.
_shutdown = False


def _handle_signal(signum: int, _frame: object) -> None:
    global _shutdown  # noqa: PLW0603
    logger.info("Received signal %d, shutting down…", signum)
    _shutdown = True


def _parse_schedule_component(
    name: str, *, default: int, minimum: int, maximum: int
) -> int:
    """Parse one schedule env var, falling back instead of crashing.

    Returns ``default`` — logging ONE error naming the variable, the raw
    value or ``<absent>``, the accepted range, and the fallback — when
    the variable is missing, non-numeric, or outside ``minimum..maximum``.
    Valid values are returned as parsed ints without any logging.
    """

    raw = os.environ.get(name)
    if raw is None:
        logger.error(
            "Invalid %s=<absent> (accepted range %d-%d) — falling back to %d",
            name,
            minimum,
            maximum,
            default,
        )
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.error(
            "Invalid %s=%r (accepted range %d-%d) — falling back to %d",
            name,
            raw,
            minimum,
            maximum,
            default,
        )
        return default
    if value < minimum or value > maximum:
        logger.error(
            "Invalid %s=%r (accepted range %d-%d) — falling back to %d",
            name,
            raw,
            minimum,
            maximum,
            default,
        )
        return default
    return value


def _run() -> None:
    """Wrapper around :func:`run_scrape` that catches exceptions."""
    try:
        inserted = run_scrape()
        logger.info("Scheduled scrape completed: %d records", inserted)
    except Exception:
        logger.exception("Scheduled scrape failed")


def main() -> None:
    _configure_logging()

    hour = _parse_schedule_component("SCRAPE_HOUR", default=2, minimum=0, maximum=23)
    minute = _parse_schedule_component(
        "SCRAPE_MINUTE", default=0, minimum=0, maximum=59
    )
    run_time = f"{hour:02d}:{minute:02d}"

    schedule.every().day.at(run_time, tz="UTC").do(_run)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "Scraper scheduler started — scrape scheduled daily at %s UTC. PID=%d",
        run_time,
        os.getpid(),
    )

    while not _shutdown:
        schedule.run_pending()
        time.sleep(30)  # check every 30 seconds

    logger.info("Scheduler stopped.")


if __name__ == "__main__":  # pragma: no cover
    main()
