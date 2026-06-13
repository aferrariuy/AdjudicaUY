"""Long-running scheduler for the scraper worker.

Runs inside the Dokploy worker container as a persistent process. The
scheduler executes :func:`scraper.main.run_scrape` once daily at a
configurable time (default 02:00 UTC = 23:00 Montevideo).

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
import sys
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


def _run() -> None:
    """Wrapper around :func:`run_scrape` that catches exceptions."""
    try:
        inserted = run_scrape()
        logger.info("Scheduled scrape completed: %d records", inserted)
    except Exception:
        logger.exception("Scheduled scrape failed")


def main() -> None:
    _configure_logging()

    hour = int(os.environ.get("SCRAPE_HOUR", "2"))
    minute = int(os.environ.get("SCRAPE_MINUTE", "0"))
    run_time = f"{hour:02d}:{minute:02d}"

    # Validate the run time before starting the loop.
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        logger.error(
            "Invalid schedule time %s — SCRAPE_HOUR must be 0-23, "
            "SCRAPE_MINUTE must be 0-59",
            run_time,
        )
        sys.exit(1)

    schedule.every().day.at(run_time).do(_run)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "Scraper scheduler started — scrape scheduled daily at %s UTC. "
        "PID=%d",
        run_time,
        os.getpid(),
    )

    while not _shutdown:
        schedule.run_pending()
        time.sleep(30)  # check every 30 seconds

    logger.info("Scheduler stopped.")


if __name__ == "__main__":  # pragma: no cover
    main()
