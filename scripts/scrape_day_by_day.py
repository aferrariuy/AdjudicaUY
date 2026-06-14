"""Test script: run scraper day-by-day for April 1 – June 12, 2026.

Each day: fetch XML + RSS with the same single-day range, join, normalize.

Usage::

    PYTHONPATH=. python scripts/scrape_day_by_day.py \\
        [--dry-run] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import TYPE_CHECKING

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_session_factory
from app.models.adjudication import Adjudication
from scraper.bcu_client import BcuClient
from scraper.joiner import JoinedRecord, join_records
from scraper.normalizer import NormalizedRecord, normalize_record
from scraper.rss_feed import (
    RssItem,
    build_per_compra_rss_url,
    fetch_and_parse_per_compra_rss,
    fetch_rss_feed,
    parse_rss_feed,
)
from scraper.xml_report import XmlAdjudication, fetch_xml_report, parse_xml_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("scrape_day_by_day")


# ── URL builders ────────────────────────────────────────────────────
def build_source_a_url(d: date) -> str:
    """Source A (XML) — same day start/end."""
    return (
        "http://www.comprasestatales.gub.uy/comprasenlinea/jboss/generarReporte"
        f"?tipo_publicacion=a"
        f"&dia_inicial={d.day}&mes_inicial={d.month}&anio_inicial={d.year}&hora_inicial=0"
        f"&dia_final={d.day}&mes_final={d.month}&anio_final={d.year}&hora_final=23"
    )


def build_source_b_url(d: date) -> str:
    """Source B (RSS) — same day start/end, matching production format."""
    ds = d.isoformat()  # YYYY-MM-DD
    return (
        f"https://www.comprasestatales.gub.uy/consultas/rss/tipo-pub/ADJ"
        f"/tipo-doc/C/tipo-fecha/PUB/rango-fecha/{ds}_{ds}/filtro-cat/CAT/tipo-orden/DESC"
    )


# Base URL for per-compra RSS (diverges from source_b_base_url which
# includes /tipo-doc/C/tipo-fecha/PUB/rango-fecha suffix).
RSS_BASE_URL = "https://www.comprasestatales.gub.uy/consultas/rss"


# ── DB insert ───────────────────────────────────────────────────────
def _to_adjudication_dict(record: NormalizedRecord) -> dict:
    return {
        "amount": record.amount,
        "currency": record.currency,
        "amount_uyu": record.amount_uyu,
        "winning_company": record.winning_company,
        "company_document": record.company_document,
        "company_document_type": record.company_document_type,
        "organism": record.organism,
        "date": record.date,
        "license_type": record.license_type,
        "article": record.article,
        "article_quantity": record.article_quantity,
        "license_link": record.license_link,
        "source_url": record.source_url,
    }


def _bulk_insert(session: Session, records: list[NormalizedRecord]) -> int:
    rows = [_to_adjudication_dict(r) for r in records]
    if not rows:
        return 0
    stmt = pg_insert(Adjudication).values(rows)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["source_url", "license_link", "date"],
    )
    try:
        session.execute(stmt)
        session.commit()
    except Exception as exc:
        session.rollback()
        # SQLAlchemy dumps the full SQL + all parameters on failure —
        # useless with 1000+ rows. Log only the cause.
        orig = exc.orig if hasattr(exc, "orig") else exc
        logger.error("DB insert failed (%d rows): %s", len(rows), orig)
        return 0
    return len(rows)


# ── Main ────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape day-by-day")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse but don't insert into DB",
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
        "--fallback-workers",
        type=int,
        default=5,
        help=(
            "Max concurrent per-compra RSS fetches in the fallback phase "
            "(default: 5). Conservative default to avoid rate-limiting "
            "the government server."
        ),
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
    # normalized records and flush when either the day count or record count
    # threshold is reached. This shrinks commit overhead from O(days) to
    # O(days/flush-interval); the 7-day / 1000-record defaults cap crash
    # data loss at ~700 records. on_conflict_do_nothing makes re-runs safe.
    buffer: list[NormalizedRecord] = []
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
            url_a = build_source_a_url(current)
            url_b = build_source_b_url(current)

            t_day_start = time.perf_counter()
            t_xml = t_rss = t_parse = t_join = 0.0
            t_fallback = t_normalize = t_insert = 0.0
            # Per-day flag: any HTTP error from the main XML/RSS fetches
            # bumps consecutive_errors exactly once at the day's end. Per-
            # compra RSS failures (next phase) don't count — they are
            # typically per-record issues, not server-health indicators.
            day_had_error = False

            # Fetch both sources for the same day, in parallel. The two
            # endpoints are independent — XML (Source A) and RSS (Source B)
            # hit different servers — so a 2-worker thread pool cuts
            # wall-clock time from t_xml + t_rss to max(t_xml, t_rss).
            # Partial failures don't abort the day: the surviving source is
            # still parsed so downstream phases (joiner, fallback, normalize)
            # can use whatever data is available.
            xml_text: str | None = None
            rss_text: str | None = None
            t_xml = 0.0
            t_rss = 0.0

            def _fetch_xml(url: str) -> str:
                return fetch_xml_report(url, client=client)

            def _fetch_rss(url: str) -> str:
                return fetch_rss_feed(url, client=client)

            with ThreadPoolExecutor(max_workers=2) as executor:
                t0_xml = time.perf_counter()
                t0_rss = time.perf_counter()
                futures = {
                    executor.submit(_fetch_xml, url_a): "xml",
                    executor.submit(_fetch_rss, url_b): "rss",
                }
                for future in as_completed(futures):
                    source = futures[future]
                    try:
                        result = future.result()
                        if source == "xml":
                            xml_text = result
                        else:
                            rss_text = result
                    except httpx.HTTPError as exc:
                        log.warning("%s fetch failed: %s", source.upper(), exc)
                        day_had_error = True
                    if source == "xml":
                        t_xml = time.perf_counter() - t0_xml
                    else:
                        t_rss = time.perf_counter() - t0_rss

            # If both fetches failed, skip this day
            if xml_text is None and rss_text is None:
                log.warning("Both XML and RSS failed for %s — skipping", current)
                if day_had_error:
                    record_error()
                current += timedelta(days=1)
                adaptive_sleep(log)
                continue

            # Parse — fall back to empty lists when one source failed so the
            # downstream pipeline still runs and can use whatever data is
            # available.
            t0 = time.perf_counter()
            if xml_text is None:
                log.warning("%s: XML failed — processing with 0 XML records", current)
                xml_records: list[XmlAdjudication] = []
            else:
                xml_records = list(parse_xml_report(xml_text))
            if rss_text is None:
                log.warning("%s: RSS failed — processing with 0 RSS items", current)
                rss_items: list[RssItem] = []
            else:
                rss_items = list(parse_rss_feed(rss_text))
            t_parse = time.perf_counter() - t0

            if not xml_records:
                if day_had_error:
                    record_error()
                current += timedelta(days=1)
                adaptive_sleep(log)
                continue

            # Join
            t0 = time.perf_counter()
            joined = join_records(xml_records, rss_items, source_url=url_a)
            t_join = time.perf_counter() - t0

            # Fallback: per-compra RSS for unmatched XML records
            #
            # Multiple XML records routinely share the same (num_compra,
            # anio_compra) — e.g. a single compra with several article
            # lines shows up as N rows in the XML report, and each one
            # misses the daily RSS feed. Fetching the per-compra RSS for
            # every row wastes the connection: the same URL returns the
            # same body. Collect unique keys first, fetch each URL once,
            # then map the result back to every record in the group.
            #
            # Fetches run in parallel with a capped ThreadPoolExecutor:
            # the shared httpx.Client is thread-safe, so 5 concurrent
            # workers reuse pooled TCP connections instead of paying
            # per-fetch handshake cost.
            t0 = time.perf_counter()
            matched_ids = {r.id_compra for r in joined}
            unmatched = [r for r in xml_records if r.id_compra not in matched_ids]

            unique_compras: dict[tuple[str, str], list] = {}
            for xml_record in unmatched:
                if not xml_record.num_compra or not xml_record.anio_compra:
                    log.warning(
                        "XML id_compra=%s has no RSS match and no "
                        "num_compra/anio_compra — skipping",
                        xml_record.id_compra,
                    )
                    continue
                key = (xml_record.num_compra, xml_record.anio_compra)
                unique_compras.setdefault(key, []).append(xml_record)

            def _fetch_per_compra(
                num_anio: tuple[str, str],
            ) -> tuple[tuple[str, str], RssItem | None]:
                num, anio = num_anio
                per_compra_url = build_per_compra_rss_url(RSS_BASE_URL, num, anio)
                return (num, anio), fetch_and_parse_per_compra_rss(
                    per_compra_url, client=client
                )

            url_cache: dict[tuple[str, str], RssItem | None] = {}
            if unique_compras:
                with ThreadPoolExecutor(
                    max_workers=max(1, args.fallback_workers)
                ) as executor:
                    # Explicit loop with annotated dict — a dict comprehension
                    # here makes mypy fall back to the Callable's first
                    # parameter type, which it then misinfers as `str` and
                    # cascades into ~10 false positives. The annotation
                    # pins the value type to the helper's real return.
                    per_compra_futures: dict[
                        Future[tuple[tuple[str, str], RssItem | None]],
                        tuple[str, str],
                    ] = {}
                    for key in unique_compras:
                        per_compra_futures[executor.submit(_fetch_per_compra, key)] = (
                            key
                        )
                    for per_compra_future in as_completed(per_compra_futures):
                        key = per_compra_futures[per_compra_future]
                        try:
                            _key, rss_match = per_compra_future.result()
                        except Exception as exc:
                            log.warning(
                                "Per-compra RSS error for num=%s, anio=%s: %s",
                                key[0],
                                key[1],
                                exc,
                            )
                            url_cache[key] = None
                            continue
                        url_cache[_key] = rss_match
                        if rss_match is None:
                            log.warning(
                                "Per-compra RSS failed for num=%s, anio=%s "
                                "— skipping %d records",
                                _key[0],
                                _key[1],
                                len(unique_compras[_key]),
                            )

            for (num, anio), xml_records_group in unique_compras.items():
                rss_match = url_cache.get((num, anio))
                if rss_match is None:
                    continue
                for xml_record in xml_records_group:
                    joined.append(
                        JoinedRecord(
                            id_compra=xml_record.id_compra,
                            fecha_pub_adj=xml_record.fecha_pub_adj,
                            id_tipocompra=xml_record.id_tipocompra,
                            id_moneda_monto_adj=xml_record.id_moneda_monto_adj,
                            nombre_comercial=xml_record.nombre_comercial,
                            nro_doc_prov=xml_record.nro_doc_prov,
                            tipo_doc_prov=xml_record.tipo_doc_prov,
                            cant_adj=xml_record.cant_adj,
                            precio_tot_imp=xml_record.precio_tot_imp,
                            desc_articulo=xml_record.desc_articulo,
                            id_moneda=xml_record.id_moneda,
                            organism=rss_match.organism,
                            license_link=rss_match.license_link,
                            source_url=url_a,
                            id_articulo=xml_record.id_articulo,
                            num_compra=xml_record.num_compra,
                            anio_compra=xml_record.anio_compra,
                        )
                    )
                    log.info(
                        "Fallback resolved id_compra=%s via per-compra RSS",
                        xml_record.id_compra,
                    )
            t_fallback = time.perf_counter() - t0

            if not joined:
                log.info(
                    "%s: %d XML, %d RSS, 0 joined — skipping",
                    current,
                    len(xml_records),
                    len(rss_items),
                )
                # "No joined" isn't an HTTP error — the fetch succeeded,
                # the data just didn't match. Don't bump consecutive_errors.
                current += timedelta(days=1)
                adaptive_sleep(log)
                continue

            # Normalize
            t0 = time.perf_counter()
            normalized: list[NormalizedRecord] = []
            for record in joined:
                try:
                    normalized.append(normalize_record(record, bcu_client))
                except Exception as exc:
                    log.warning(
                        "Normalize failed id_compra=%s: %s", record.id_compra, exc
                    )
            t_normalize = time.perf_counter() - t0

            if not normalized:
                # Normalization failed on every record — not an HTTP error.
                current += timedelta(days=1)
                adaptive_sleep(log)
                continue

            days_with_data += 1

            # Insert or dry-run. Records accumulate in a buffer that flushes
            # when either the day or record threshold is hit; this shrinks
            # commit overhead from O(days) to O(days/flush-interval).
            # Dry-run skips the buffer entirely — we just count records.
            t0 = time.perf_counter()
            if args.dry_run:
                log.info(
                    "[DRY RUN] %s: %d XML → %d joined → %d normalized",
                    current,
                    len(xml_records),
                    len(joined),
                    len(normalized),
                )
                total += len(normalized)
            else:
                buffer.extend(normalized)
                days_since_flush += 1
                log.info(
                    "%s: %d XML → %d joined → %d normalized "
                    "(buffered=%d, days_since_flush=%d)",
                    current,
                    len(xml_records),
                    len(joined),
                    len(normalized),
                    len(buffer),
                    days_since_flush,
                )
                if (
                    len(buffer) >= args.flush_size
                    or days_since_flush >= args.flush_interval
                ):
                    inserted = _bulk_insert(session, buffer)
                    log.info(
                        "Flushed %d records (total=%d)",
                        inserted,
                        total + inserted,
                    )
                    total += inserted
                    buffer.clear()
                    days_since_flush = 0
            t_insert = time.perf_counter() - t0

            t_day = time.perf_counter() - t_day_start
            log.info(
                "TIMING %s: day=%.2fs xml=%.2fs rss=%.2fs parse=%.2fs "
                "join=%.2fs fallback=%.2fs normalize=%.2fs insert=%.2fs",
                current,
                t_day,
                t_xml,
                t_rss,
                t_parse,
                t_join,
                t_fallback,
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
        # on_conflict_do_nothing on _bulk_insert keeps a re-run idempotent
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
