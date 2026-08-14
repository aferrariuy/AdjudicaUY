"""Test script: run scraper day-by-day for April 1 – June 12, 2026.

Each day: fetch the XML report, parse, enrich via the static
``(id_inciso, id_ue)`` organism lookup, build ``license_link`` from
``id_compra``, normalize via BCU, and insert.

Usage::

    PYTHONPATH=. python scripts/scrape_day_by_day.py \\
        [--dry-run] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date, timedelta
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_session_factory
from scraper.bcu_client import BcuClient
from scraper.main import (
    build_source_a_url,
    enrich_xml_compra,
)
from scraper.normalizer import (
    CompraRow,
    normalize_compra,
)
from scraper.persistence import bulk_insert as _bulk_insert_hard
from scraper.xml_report import (
    XmlCompra,
    fetch_xml_report,
    parse_xml_report,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("scrape_day_by_day")


# ── DB insert ───────────────────────────────────────────────────────
# The dict projections and the canonical insert live in
# :mod:`scraper.persistence`. The day-by-day backfill script
# prefers **soft-failure** — a failed flush should not abort the
# whole run, so this thin wrapper catches the ``SQLAlchemyError``
# the canonical ``bulk_insert`` propagates and turns it into a
# logged warning + a 0 return. The canonical worker
# (``scraper.main``) calls ``bulk_insert`` directly with no
# wrapper because it wants the orchestrating cron / Dokploy job
# to see the DB error.


def _bulk_insert(session: Session, rows: list[CompraRow]) -> int:
    """Soft-failure wrapper around :func:`scraper.persistence.bulk_insert`.

    Rolls the session back on any exception (matching the legacy
    behavior) and logs only ``exc.orig`` — SQLAlchemy dumps the
    full SQL + all parameters on failure, useless with 1000+ rows.
    Returns 0 on error so the backfill can keep walking days.
    """
    if not rows:
        return 0
    try:
        return _bulk_insert_hard(session, rows)
    except Exception as exc:
        session.rollback()
        orig = getattr(exc, "orig", exc)
        logger.error("DB insert failed (%d rows): %s", len(rows), orig)
        return 0


# ── Main ────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape day-by-day")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse but don't insert into DB",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed per-day logs (default: warnings + summary only)",
    )
    parser.add_argument("--start", default="2026-04-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-06-12", help="End date (YYYY-MM-DD)")
    parser.add_argument(
        "--client-timeout",
        type=float,
        default=30.0,
        help="Per-request HTTP timeout in seconds (default: 30.0)",
    )
    parser.add_argument(
        "--pool-size",
        type=int,
        default=20,
        help="Max connections in pool (default: 20)",
    )
    parser.add_argument(
        "--pool-keepalive",
        type=int,
        default=10,
        help="Max keepalive connections (default: 10)",
    )
    parser.add_argument(
        "--base-delay",
        type=float,
        default=0.1,
        help=(
            "Base delay between days in seconds (default: 0.1). The delay "
            "doubles on each consecutive HTTP error, capped at 5.0s."
        ),
    )
    parser.add_argument(
        "--skip-sleep",
        action="store_true",
        help="Skip all inter-day sleeps (useful for benchmarks).",
    )
    parser.add_argument(
        "--flush-interval",
        type=int,
        default=7,
        help=(
            "Flush the insert buffer every N days (default: 7). Whichever "
            "threshold (days or records) is reached first triggers the flush."
        ),
    )
    parser.add_argument(
        "--flush-size",
        type=int,
        default=1000,
        help=(
            "Flush the insert buffer every N records (default: 1000). "
            "Whichever threshold (days or records) is reached first "
            "triggers the flush."
        ),
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)
    # Always show our summary/operator messages regardless of --verbose.
    # Per-day noise lives on child ``day.*`` loggers (suppressed by root
    # WARNING); this keeps DONE/TIMING visible without --verbose.
    logger.setLevel(logging.INFO)

    t_overall_start = time.perf_counter()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    logger.info("Range: %s to %s (%d days)", start, end, (end - start).days + 1)

    settings = get_settings()
    session = get_session_factory()()
    # Shared httpx.Client — connection pooling + keep-alive for all fetches.
    # Conservative pool (20 max / 10 keepalive) to be polite to the
    # government server; thread-safe for the parallel phases that follow.
    # Pool limits are exposed via --pool-size / --pool-keepalive so the
    # operator can tune them without editing the script.
    client = httpx.Client(
        timeout=args.client_timeout,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        },
        limits=httpx.Limits(
            max_connections=args.pool_size,
            max_keepalive_connections=args.pool_keepalive,
            keepalive_expiry=30,
        ),
    )
    bcu_client = BcuClient(
        settings.bcu_api_url, timeout=args.client_timeout, client=client
    )
    total = 0
    days_with_data = 0
    # Batch insert buffer: instead of committing after every day, accumulate
    # CompraRow records and flush when either the day count or record
    # count threshold is reached. This shrinks commit overhead from
    # O(days) to O(days/flush-interval); the 7-day / 1000-record
    # defaults cap crash data loss at ~700 records. on_conflict_do_nothing
    # makes re-runs safe.
    buffer: list[CompraRow] = []
    days_since_flush = 0

    # Adaptive sleep replaces the fixed time.sleep() calls between days.
    # The base delay is the floor; on consecutive HTTP errors the delay
    # doubles exponentially (capped at 5s) so transient server issues
    # self-throttle the scraper without losing throughput on the happy
    # path. Counters are closure-local — no module-level state.
    consecutive_errors = 0

    def adaptive_sleep(log: logging.Logger) -> None:
        """Sleep between days with exponential back-off on consecutive errors.

        Delay is ``args.base_delay * 2^min(consecutive_errors, 5)``,
        capped at 5.0s. No-op when ``--skip-sleep`` is set. The chosen
        delay is logged at DEBUG level so benchmarks can verify the
        back-off curve.
        """
        nonlocal consecutive_errors
        if args.skip_sleep:
            return
        delay = args.base_delay * (2 ** min(consecutive_errors, 5))
        delay = min(delay, 5.0)
        time.sleep(delay)
        log.debug("Sleep %.2fs (consecutive_errors=%d)", delay, consecutive_errors)

    def record_error() -> None:
        """Bump the consecutive-errors counter (called on HTTP errors)."""

        nonlocal consecutive_errors
        consecutive_errors += 1

    def record_success() -> None:
        """Reset the consecutive-errors counter (called on day success)."""

        nonlocal consecutive_errors
        consecutive_errors = 0

    try:
        current = start
        while current <= end:
            log = logging.getLogger(f"day.{current.isoformat()}")
            url_a = build_source_a_url(settings.source_a_base_url, current)

            t_day_start = time.perf_counter()
            t_xml = t_parse = t_normalize = t_insert = 0.0
            # Per-day flag: any HTTP error from the main XML fetch bumps
            # consecutive_errors exactly once at the day's end.
            day_had_error = False

            xml_text: bytes | None = None

            # ------------------------------------------------------------------
            # 1. Fetch the XML report
            # ------------------------------------------------------------------
            t0_xml = time.perf_counter()
            try:
                xml_text = fetch_xml_report(url_a, client=client)
            except httpx.HTTPError as exc:
                log.warning("XML fetch failed: %s", exc)
                day_had_error = True
            t_xml = time.perf_counter() - t0_xml

            if xml_text is None:
                log.warning("XML fetch failed for %s — skipping", current)
                if day_had_error:
                    record_error()
                current += timedelta(days=1)
                adaptive_sleep(log)
                continue

            # ------------------------------------------------------------------
            # 2. Parse + enrich
            # ------------------------------------------------------------------
            # Enrichment is the inline replacement for the historical
            # RSS-join + per-compra fallback: resolve the organism via
            # the static ``(id_inciso, id_ue)`` lookup and build
            # ``license_link`` deterministically from ``id_compra``.
            t0 = time.perf_counter()
            xml_compras: list[XmlCompra] = list(parse_xml_report(xml_text))
            t_parse = time.perf_counter() - t0

            if not xml_compras:
                if day_had_error:
                    record_error()
                current += timedelta(days=1)
                adaptive_sleep(log)
                continue

            enriched = [
                (compra, enrich_xml_compra(compra, source_url=url_a))
                for compra in xml_compras
            ]

            # ------------------------------------------------------------------
            # 3. Normalize
            # ------------------------------------------------------------------
            t0 = time.perf_counter()
            normalized: list[CompraRow] = []
            for compra, enrichment in enriched:
                try:
                    normalized.append(normalize_compra(compra, enrichment, bcu_client))
                except Exception as exc:
                    log.warning(
                        "Normalize failed id_compra=%s: %s", compra.id_compra, exc
                    )
            t_normalize = time.perf_counter() - t0

            if not normalized:
                # Normalization failed on every record — not an HTTP error.
                current += timedelta(days=1)
                adaptive_sleep(log)
                continue

            days_with_data += 1

            # ------------------------------------------------------------------
            # 4. Insert or dry-run
            # ------------------------------------------------------------------
            t0 = time.perf_counter()
            if args.dry_run:
                total_adjs = sum(len(c.adjudicaciones) for c in normalized)
                log.debug(
                    "[DRY RUN] %s: %d XML → %d compras (%d adjudicaciones)",
                    current,
                    len(xml_compras),
                    len(normalized),
                    total_adjs,
                )
                total += len(normalized)
            else:
                buffer.extend(normalized)
                days_since_flush += 1
                log.debug(
                    "%s: %d XML → %d compras (buffered=%d, days_since_flush=%d)",
                    current,
                    len(xml_compras),
                    len(normalized),
                    len(buffer),
                    days_since_flush,
                )
                if (
                    len(buffer) >= args.flush_size
                    or days_since_flush >= args.flush_interval
                ):
                    inserted = _bulk_insert(session, buffer)
                    log.debug(
                        "Flushed %d records (total=%d)",
                        inserted,
                        total + inserted,
                    )
                    total += inserted
                    buffer.clear()
                    days_since_flush = 0
            t_insert = time.perf_counter() - t0

            t_day = time.perf_counter() - t_day_start
            log.debug(
                "TIMING %s: day=%.2fs xml=%.2fs parse=%.2fs "
                "normalize=%.2fs insert=%.2fs",
                current,
                t_day,
                t_xml,
                t_parse,
                t_normalize,
                t_insert,
            )

            # Day processed end-to-end — reset the back-off counter so the
            # adaptive sleep below uses the base delay, not a stale back-off.
            record_success()
            current += timedelta(days=1)
            adaptive_sleep(log)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        # Final flush: anything left in the buffer must be committed so a
        # graceful exit (or a crash right after) doesn't lose data. The
        # on_conflict_do_nothing on bulk_insert keeps a re-run idempotent
        # in case the process was killed between commit and process exit.
        if buffer and not args.dry_run:
            inserted = _bulk_insert(session, buffer)
            total += inserted
            logger.info("Final flush: %d records (%d total)", inserted, total)
            buffer.clear()
        client.close()
        bcu_client.close()
        session.close()

    logger.info(
        "═══ DONE: %d records from %d days with data ═══", total, days_with_data
    )
    logger.info("═══ TIMING SUMMARY ═══")
    logger.info("Total wall time: %.2fs", time.perf_counter() - t_overall_start)
    logger.info("Days with data: %d", days_with_data)
    logger.info("Total records: %d", total)


if __name__ == "__main__":
    main()
