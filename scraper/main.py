"""Scraper worker entry point.

The ``run_scrape`` function is the single public entry point used by the
worker container (and by ``python -m scraper.main``). It implements the
end-to-end pipeline::

    build URLs → fetch XML → fetch RSS → parse → join → normalize → bulk insert

The pipeline is fail-soft at the source level: a missing or malformed
source is logged and the run returns without inserting anything for that
source. The pipeline is fail-hard at the database level: any DB error
propagates so the orchestrating cron / Dokploy job can surface it.

Date handling
-------------
``run_scrape`` accepts an optional ``[start_date, end_date]`` range. When
omitted, the function defaults to **today** (``start_hour=0``,
``end_hour=23``). The per-day URL is built from the base URLs configured
in :class:`app.config.Settings` — see :func:`build_source_a_url` and
:func:`build_source_b_url`.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.database import get_session_factory
from app.models.adjudication import Adjudication
from scraper.bcu_client import BcuClient
from scraper.joiner import join_records
from scraper.normalizer import NormalizedRecord, normalize_record
from scraper.rss_feed import fetch_rss_feed, parse_rss_feed
from scraper.xml_report import fetch_xml_report, parse_xml_report

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------


def build_source_a_url(
    base_url: str,
    d: date,
    start_hour: int = 0,
    end_hour: int = 23,
) -> str:
    """Build the Source A (XML report) URL for a single day.

    The Source A endpoint accepts a date range via query parameters. We
    always query a single day at a time so each loop iteration in
    :func:`run_scrape` is a well-scoped unit of work.

    Parameters
    ----------
    base_url:
        The Source A base URL (no query string).
    d:
        The day to fetch. Both ``dia_inicial`` and ``dia_final`` are set
        to ``d`` so the upstream endpoint returns records published on
        that single day.
    start_hour, end_hour:
        Inclusive hour-of-day bounds applied to the day. Defaults to the
        full 24-hour window.
    """

    return (
        f"{base_url}"
        f"?tipo_publicacion=a"
        f"&dia_inicial={d.day}&mes_inicial={d.month}&anio_inicial={d.year}"
        f"&hora_inicial={start_hour}"
        f"&dia_final={d.day}&mes_final={d.month}&anio_final={d.year}"
        f"&hora_final={end_hour}"
    )


def build_source_b_url(base_url: str, d_start: date, d_end: date) -> str:
    """Build the Source B (RSS feed) URL for a date range.

    The Source B endpoint takes the date range as a path segment
    ``rango-fecha/<start>_<end>``. We fetch the **whole** range in a
    single request (per day would multiply requests without adding
    information — the RSS feed is a per-publication list, not a
    per-day partition).

    Parameters
    ----------
    base_url:
        The Source B base URL, up to (and including) the ``rango-fecha``
        path segment.
    d_start, d_end:
        Inclusive bounds of the range, formatted as ISO ``YYYY-MM-DD``.
    """

    s = d_start.isoformat()
    e = d_end.isoformat()
    return f"{base_url}/{s}_{e}/filtro-cat/CAT/tipo-orden/DESC"


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


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
        "article_id": record.article_id,
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


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _run_scrape_for_day(
    *,
    target_day: date,
    range_start: date,
    range_end: date,
    source_a_base_url: str,
    source_b_base_url: str,
    session: Session,
    bcu_client: BcuClient,
    start_hour: int,
    end_hour: int,
) -> int:
    """Run the full pipeline for a single day.

    Returns the number of records inserted for this day, or ``0`` when
    the day produced no data (empty XML, empty RSS, no join, or no
    surviving normalization). Raises :class:`SQLAlchemyError` on DB
    failures so the caller can roll back and surface the error.
    """

    log = logging.getLogger("scraper.run_scrape")

    url_a = build_source_a_url(
        source_a_base_url,
        target_day,
        start_hour,
        end_hour,
    )
    url_b = build_source_b_url(source_b_base_url, range_start, range_end)

    # ------------------------------------------------------------------
    # 1. Fetch sources
    # ------------------------------------------------------------------
    try:
        xml_text = fetch_xml_report(url_a)
        log.info("Fetched XML report from %s", url_a)
    except httpx.HTTPError as exc:
        log.error("XML report fetch failed for %s: %s; skipping day", target_day, exc)
        return 0

    try:
        rss_text = fetch_rss_feed(url_b)
        log.info("Fetched RSS feed from %s", url_b)
    except httpx.HTTPError as exc:
        log.error("RSS feed fetch failed for %s: %s; skipping day", target_day, exc)
        return 0

    # ------------------------------------------------------------------
    # 2. Parse
    # ------------------------------------------------------------------
    xml_records = list(parse_xml_report(xml_text))
    rss_items = list(parse_rss_feed(rss_text))
    log.info(
        "Parsed %d XML adjudications, %d RSS items for %s",
        len(xml_records),
        len(rss_items),
        target_day,
    )

    if not xml_records:
        log.info("No XML records for %s; nothing to insert", target_day)
        return 0
    if not rss_items:
        log.info("No RSS items for %s; cannot enrich records, skipping", target_day)
        return 0

    # ------------------------------------------------------------------
    # 3. Join
    # ------------------------------------------------------------------
    # Use the per-day URL as ``source_url`` so the
    # ``(source_url, license_link, date)`` unique constraint dedupes
    # correctly: same day + same link = conflict (skip); different day
    # = different source_url = insert (even if the same adjudication
    # appears on multiple days).
    joined = join_records(
        xml_records,
        rss_items,
        source_url=url_a,
    )
    if not joined:
        log.info("Join produced no records for %s; nothing to insert", target_day)
        return 0

    # ------------------------------------------------------------------
    # 4. Normalize (BCU rate fetch + currency conversion)
    # ------------------------------------------------------------------
    normalized: list[NormalizedRecord] = []
    for record in joined:
        try:
            normalized.append(normalize_record(record, bcu_client))
        except Exception as exc:  # BcuError, malformed data, etc.
            log.warning(
                "Normalization failed for id_compra=%s on %s: %s",
                record.id_compra,
                target_day,
                exc,
            )

    if not normalized:
        log.info(
            "No records survived normalization for %s; nothing to insert", target_day
        )
        return 0

    # ------------------------------------------------------------------
    # 5. Bulk insert (idempotent)
    # ------------------------------------------------------------------
    try:
        inserted = _bulk_insert(session, normalized)
    except SQLAlchemyError as exc:
        log.error("Database insert failed for %s: %s", target_day, exc)
        session.rollback()
        raise

    log.info("Inserted %d records for %s", inserted, target_day)
    return inserted


def run_scrape(
    *,
    session: Session | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    start_hour: int = 0,
    end_hour: int = 23,
) -> int:
    """Execute one full scrape run. Returns the total number of records submitted.

    Parameters
    ----------
    session:
        Optional SQLAlchemy ``Session``. When ``None``, a session is
        borrowed from :func:`app.database.get_session_factory` and closed
        before returning. Tests pass in their own session.
    start_date, end_date:
        Inclusive date range to scrape. When both are ``None`` (the
        default), the run scrapes **today only**. When only one is
        ``None``, the missing one defaults to the other (single-day run).
    start_hour, end_hour:
        Inclusive hour-of-day bounds applied to Source A per day. The
        defaults (``0`` and ``23``) cover a full 24-hour window.

    Returns
    -------
    int
        The total number of normalized records passed to ``ON CONFLICT
        DO NOTHING`` across all days in the range. Existing rows are
        silently skipped by the database.

    Notes
    -----
    The :class:`BcuClient` is constructed once per call and shared across
    every day in the range so its in-memory rate cache is reused.
    """

    settings = get_settings()
    log = logging.getLogger("scraper.run_scrape")

    # ------------------------------------------------------------------
    # Resolve date range
    # ------------------------------------------------------------------
    if start_date is None and end_date is None:
        start_date = end_date = date.today()
    elif start_date is None:
        start_date = end_date  # type: ignore[assignment]
    elif end_date is None:
        end_date = start_date

    if start_date is None or end_date is None:
        raise RuntimeError("date range resolution failed — this should never happen")

    log.info(
        "Scraper run starting: range=%s..%s start_hour=%d end_hour=%d",
        start_date,
        end_date,
        start_hour,
        end_hour,
    )

    owns_session = session is None
    if owns_session:
        session = get_session_factory()()

    total_inserted = 0
    try:
        # One BCU client for the entire range — its (bcu_code, date)
        # cache is the whole point of having a long-lived client.
        with BcuClient(settings.bcu_api_url) as bcu_client:
            current = start_date
            while current <= end_date:
                inserted = _run_scrape_for_day(
                    target_day=current,
                    range_start=start_date,
                    range_end=end_date,
                    source_a_base_url=settings.source_a_base_url,
                    source_b_base_url=settings.source_b_base_url,
                    session=session,  # type: ignore[arg-type]
                    bcu_client=bcu_client,
                    start_hour=start_hour,
                    end_hour=end_hour,
                )
                total_inserted += inserted
                current += timedelta(days=1)

        log.info(
            "Scraper run complete: %d records submitted to DB across %s..%s",
            total_inserted,
            start_date,
            end_date,
        )
        return total_inserted

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
