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
from scraper.joiner import join_records
from scraper.normalizer import NormalizedRecord, normalize_record
from scraper.rss_feed import fetch_rss_feed, parse_rss_feed
from scraper.xml_report import fetch_xml_report, parse_xml_report

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
        f"&dia_final={d.day}&mes_final={d.month}&anio_final={d.year}&hora_final=1"
    )


def build_source_b_url(d: date) -> str:
    """Source B (RSS) — same day start/end, matching production format."""
    ds = d.isoformat()  # YYYY-MM-DD
    return (
        f"https://www.comprasestatales.gub.uy/consultas/rss/tipo-pub/ADJ"
        f"/tipo-doc/C/tipo-fecha/PUB/rango-fecha/{ds}_{ds}/filtro-cat/CAT/tipo-orden/DESC"
    )


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
    session.execute(stmt)
    session.commit()
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
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    logger.info("Range: %s to %s (%d days)", start, end, (end - start).days + 1)

    settings = get_settings()
    session = get_session_factory()()
    bcu_client = BcuClient(settings.bcu_api_url)
    total = 0
    days_with_data = 0

    try:
        current = start
        while current <= end:
            log = logging.getLogger(f"day.{current.isoformat()}")
            url_a = build_source_a_url(current)
            url_b = build_source_b_url(current)

            # Fetch both sources for the same day
            try:
                xml_text = fetch_xml_report(url_a)
            except httpx.HTTPError as exc:
                log.warning("XML failed: %s — skipping", exc)
                current += timedelta(days=1)
                time.sleep(1.0)
                continue

            try:
                rss_text = fetch_rss_feed(url_b)
            except httpx.HTTPError as exc:
                log.warning("RSS failed: %s — skipping", exc)
                current += timedelta(days=1)
                time.sleep(1.0)
                continue

            # Parse
            xml_records = list(parse_xml_report(xml_text))
            rss_items = list(parse_rss_feed(rss_text))

            if not xml_records:
                current += timedelta(days=1)
                time.sleep(0.5)
                continue

            # Join
            joined = join_records(xml_records, rss_items, source_url=url_a)
            if not joined:
                log.info(
                    "%s: %d XML, %d RSS, 0 joined — skipping",
                    current,
                    len(xml_records),
                    len(rss_items),
                )
                current += timedelta(days=1)
                time.sleep(0.5)
                continue

            # Normalize
            normalized: list[NormalizedRecord] = []
            for record in joined:
                try:
                    normalized.append(normalize_record(record, bcu_client))
                except Exception as exc:
                    log.warning(
                        "Normalize failed id_compra=%s: %s", record.id_compra, exc
                    )

            if not normalized:
                current += timedelta(days=1)
                time.sleep(0.5)
                continue

            days_with_data += 1

            # Insert or dry-run
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
                inserted = _bulk_insert(session, normalized)
                log.info(
                    "%s: %d XML → %d joined → %d inserted",
                    current,
                    len(xml_records),
                    len(joined),
                    inserted,
                )
                total += inserted

            current += timedelta(days=1)
            time.sleep(1.0)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        bcu_client.close()
        session.close()

    logger.info(
        "═══ DONE: %d records from %d days with data ═══", total, days_with_data
    )


if __name__ == "__main__":
    main()
