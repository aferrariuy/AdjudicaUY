"""Long-running scheduler for the scraper worker.

Runs inside the Dokploy worker container as a persistent process. The
scheduler executes :func:`scraper.main.run_scrape` once daily at a
configurable time (default 02:00 UTC = 23:00 Montevideo).

Schedule configuration is tolerant by design: a missing, non-numeric,
or out-of-range ``SCRAPE_HOUR`` / ``SCRAPE_MINUTE`` falls back to the
default (02:00) with one logged error instead of crashing the worker.
The daily job is registered with an explicit UTC timezone.

Configuration (environment variables):
    SCRAPE_HOUR           — Hour of day to run (0-23, default: 2)
    SCRAPE_MINUTE         — Minute of the hour (0-59, default: 0)
    WORKER_HEARTBEAT_FILE — Heartbeat path (default: /tmp/worker.heartbeat)
    WORKER_LAST_RUN_FILE  — Last-run marker path (default: /tmp/worker.last-run.json)

Usage::

    python -m scraper.scheduler
"""

from __future__ import annotations

import json
import logging
import os
import signal
import tempfile
import time
from datetime import date, datetime, timezone

import schedule

from app.config import get_settings
from app.database import get_session_factory
from app.formatting import company_path, organism_path
from app.services.catalog import entities_active_between
from scraper.indexnow import submit_urls
from scraper.main import _configure_logging, run_scrape, scrape_day

logger = logging.getLogger(__name__)

# Default marker paths — pinned by the docker-compose worker contract; both
# live under the container's writable /tmp tmpfs (read_only: true). The /tmp
# literals are deliberate: they ARE the pinned defaults.
_HEARTBEAT_FILE_DEFAULT = "/tmp/worker.heartbeat"  # noqa: S108  # nosec B108
_LAST_RUN_FILE_DEFAULT = "/tmp/worker.last-run.json"  # noqa: S108  # nosec B108

# Graceful shutdown flag — set by SIGTERM/SIGINT handlers so the main
# loop can exit cleanly when Docker stops the container.
_shutdown = False


def _handle_signal(signum: int, _frame: object) -> None:
    global _shutdown  # noqa: PLW0603
    logger.info("Received signal %d, shutting down…", signum)
    _shutdown = True


def _heartbeat_path() -> str:
    return os.environ.get("WORKER_HEARTBEAT_FILE", _HEARTBEAT_FILE_DEFAULT)


def _last_run_path() -> str:
    return os.environ.get("WORKER_LAST_RUN_FILE", _LAST_RUN_FILE_DEFAULT)


def _touch_heartbeat(path: str) -> None:
    with open(path, "a", encoding="utf-8"):
        pass
    os.utime(path, None)


def _heartbeat_is_fresh(path: str, threshold: float = 300.0) -> bool:
    try:
        return os.path.isfile(path) and time.time() - os.path.getmtime(path) < threshold
    except OSError:
        return False


def _write_last_run(path: str, count: int) -> None:
    """Atomically replace the last-run marker with a successful scrape record."""
    parent = os.path.dirname(path) or "."
    prefix = f".{os.path.basename(path)}."
    payload = {
        "completed_at": datetime.now(timezone.utc)  # noqa: UP017 — pinned contract uses timezone.utc
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "record_count": count,
    }
    fd, tmp_path = tempfile.mkstemp(prefix=prefix, dir=parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = ""
    finally:
        if tmp_path:
            try:  # noqa: SIM105 — pinned cleanup contract unlinks the leftover temp file
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


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


def _notify_indexnow(day: date) -> None:
    """Announce the pages the scrape of ``day`` touched, best-effort.

    The notification is a courtesy hint to search engines, so it is deliberately
    isolated from the run's outcome: a missing key, an unreachable endpoint or a
    database hiccup is logged and swallowed. A scrape that stored its records must
    never be reported as failed because a search engine was unavailable.

    ``day`` is the day the run actually scraped, handed in by the caller so that the
    query window and the stored rows share one clock. Reading the local clock here
    instead compares a Montevideo day against a UTC one: at the default 02:00 UTC
    schedule the container is still on the previous Uruguayan day, so the window
    would never overlap the rows just written and only the index would be announced.
    """

    try:
        settings = get_settings()
        if not settings.indexnow_key:
            return

        with get_session_factory()() as session:
            organisms, companies = entities_active_between(
                session, start_date=day, end_date=day
            )
        urls = [
            f"{settings.site_url}/",
            *(f"{settings.site_url}{organism_path(name)}" for name in organisms),
            *(
                f"{settings.site_url}{company_path(company_type, company_number)}"
                for company_type, company_number in companies
            ),
        ]
        result = submit_urls(
            urls, site_url=settings.site_url, key=settings.indexnow_key
        )
        logger.info(
            "IndexNow notification: status=%s urls=%d dropped=%d",
            result.status,
            result.url_count,
            result.dropped_count,
        )
    except Exception:
        logger.exception("IndexNow notification failed")


def _run() -> None:
    """Run one scheduled scrape, recording a last-run marker on success."""

    # One clock for the whole run: the scrape and the IndexNow window must agree on
    # which day was covered. The container's local clock is not that day — it runs in
    # UTC while the reports are dated in Montevideo.
    day = scrape_day()
    try:
        inserted = run_scrape(start_date=day, end_date=day)
    except Exception:
        logger.exception("Scheduled scrape failed")
        return

    logger.info("Scheduled scrape completed: %d records", inserted)
    path = _last_run_path()
    try:
        _write_last_run(path, inserted)
    except Exception:
        logger.exception("Last-run marker write failed at %s", path)

    # After the marker: the notification can spend ~13s in retries, and the marker
    # is the signal an operator reads, so it must not wait on a courtesy call to a
    # search engine.
    _notify_indexnow(day)


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
        path = _heartbeat_path()
        try:
            _touch_heartbeat(path)
        except Exception:
            logger.exception("Heartbeat touch failed at %s", path)
        schedule.run_pending()
        time.sleep(30)  # check every 30 seconds

    logger.info("Scheduler stopped.")


if __name__ == "__main__":  # pragma: no cover
    main()
