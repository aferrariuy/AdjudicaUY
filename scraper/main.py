"""Scraper worker entry point.

The ``run_scrape`` function is the single public entry point used by the
worker container (and by ``python -m scraper.main``). It implements the
end-to-end pipeline::

    fetch XML → fetch RSS → parse → join → normalize → bulk insert

The pipeline is fail-soft at the source level: a missing or malformed
source is logged and the run returns without inserting anything for that
source. The pipeline is fail-hard at the database level: any DB error
propagates so the orchestrating cron / Dokploy job can surface it.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable
from datetime import date
from typing import Any

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_session_factory
from app.models.adjudication import Adjudication
from scraper.bcu_client import BcuClient
from scraper.joiner import join_records
from scraper.normalizer import NormalizedRecord, normalize_record
from scraper.rss_feed import fetch_rss_feed, parse_rss_feed
from scraper.xml_report import fetch_xml_report, parse_xml_report

logger = logging.getLogger(__name__)


def _to_adjudication_dict(record: NormalizedRecord) -> dict[str, Any]:
    """Map a :class:`NormalizedRecord` to the columns of ``Adjudication``.

    Centralizing the mapping here keeps the model decoupled from the
    scraper package — the only place that knows the model's exact column
    names is this function.
    """

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


def _bulk_insert(session: Session, records: Iterable[NormalizedRecord]) -> int:
    """Insert ``records`` into the ``adjudications`` table, idempotently.

    Uses PostgreSQL's ``ON CONFLICT DO NOTHING`` against the unique
    constraint ``(source_url, license_link, date)`` so a re-run of the
    scraper on the same data is a no-op. Returns the number of rows
    passed to the database (not the number actually inserted — the
    database does not report the latter without an additional round-trip).
    """

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


def run_scrape(*, session: Session | None = None) -> int:
    """Execute one full scrape run. Returns the number of records attempted.

    Parameters
    ----------
    session:
        Optional SQLAlchemy ``Session``. When ``None``, a session is
        borrowed from :func:`app.database.get_session_factory` and closed
        before returning. Tests pass in their own session.

    Returns
    -------
    int
        The number of normalized records passed to ``ON CONFLICT DO
        NOTHING`` for insertion. Existing rows are silently skipped by the
        database.
    """

    settings = get_settings()
    log = logging.getLogger("scraper.run_scrape")
    log.info("Scraper run starting")

    owns_session = session is None
    if owns_session:
        session = get_session_factory()()

    inserted = 0
    try:
        # ------------------------------------------------------------------
        # 1. Fetch sources
        # ------------------------------------------------------------------
        try:
            xml_text = fetch_xml_report(settings.source_a_url)
            log.info("Fetched XML report from %s", settings.source_a_url)
        except httpx.HTTPError as exc:
            log.error("XML report fetch failed; aborting run: %s", exc)
            return 0

        try:
            rss_text = fetch_rss_feed(settings.source_b_url)
            log.info("Fetched RSS feed from %s", settings.source_b_url)
        except httpx.HTTPError as exc:
            log.error("RSS feed fetch failed; aborting run: %s", exc)
            return 0

        # ------------------------------------------------------------------
        # 2. Parse
        # ------------------------------------------------------------------
        xml_records = list(parse_xml_report(xml_text))
        rss_items = list(parse_rss_feed(rss_text))
        log.info("Parsed %d XML adjudications, %d RSS items", len(xml_records), len(rss_items))

        if not xml_records:
            log.warning("No XML records parsed; nothing to insert")
            return 0
        if not rss_items:
            log.warning("No RSS items parsed; cannot enrich records, aborting")
            return 0

        # ------------------------------------------------------------------
        # 3. Join
        # ------------------------------------------------------------------
        joined = join_records(
            xml_records,
            rss_items,
            source_url=settings.source_a_url,
        )
        if not joined:
            log.warning("Join produced no records; nothing to insert")
            return 0

        # ------------------------------------------------------------------
        # 4. Normalize (BCU rate fetch + currency conversion)
        # ------------------------------------------------------------------
        with BcuClient(settings.bcu_api_url) as bcu_client:
            normalized: list[NormalizedRecord] = []
            for record in joined:
                try:
                    normalized.append(normalize_record(record, bcu_client))
                except Exception as exc:  # BcuError, malformed data, etc.
                    log.warning(
                        "Normalization failed for id_compra=%s: %s",
                        record.id_compra, exc,
                    )

        if not normalized:
            log.warning("No records survived normalization; nothing to insert")
            return 0

        # ------------------------------------------------------------------
        # 5. Bulk insert (idempotent)
        # ------------------------------------------------------------------
        try:
            inserted = _bulk_insert(session, normalized)
        except SQLAlchemyError as exc:
            log.error("Database insert failed: %s", exc)
            session.rollback()
            raise

        log.info("Scraper run complete: %d records submitted to DB", inserted)
        return inserted

    finally:
        if owns_session and session is not None:
            session.close()


def _configure_logging() -> None:
    """Configure root logging for the worker process."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


if __name__ == "__main__":  # pragma: no cover
    _configure_logging()
    try:
        result = run_scrape()
    except Exception:
        logger.exception("Scraper run crashed")
        sys.exit(1)
    sys.exit(0 if result >= 0 else 1)
