"""Scraper worker entry point.

The ``run_scrape`` function is the single public entry point used by the
worker container (and by ``python -m scraper.main``). It implements the
end-to-end pipeline::

    build URL → fetch XML → parse → resolve organism (lookup) → normalize → bulk insert

The pipeline is fail-soft at the source level: a missing or malformed
source is logged and the run returns without inserting anything for that
source. The pipeline is fail-hard at the database level: any DB error
propagates so the orchestrating cron / Dokploy job can surface it.

Date handling
-------------
``run_scrape`` accepts an optional ``[start_date, end_date]`` range. When
omitted, the function defaults to **today** (``start_hour=0``,
``end_hour=23``). The per-day URL is built from the base URL configured
in :class:`app.config.Settings` — see :func:`build_source_a_url`.

Persistence
-----------
Records are inserted into the new ``compra`` / ``adjudicacion`` /
``oferente`` tables. Idempotency is enforced via ``ON CONFLICT DO
NOTHING`` on ``compra.id_compra`` (the natural key from the upstream
XML). A re-run on the same data is a no-op at the parent level;
existing children stay attached to the existing compra row.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.database import get_session_factory
from app.formatting import build_license_link
from scraper.bcu_client import BcuClient, BcuError
from scraper.normalizer import (
    CompraEnrichment,
    CompraRow,
    NormalizationError,
    normalize_compra,
)
from scraper.organism_lookup import UNKNOWN_ORGANISM_PREFIX, resolve_organism
from scraper.persistence import bulk_insert
from scraper.ucc_lookup import resolve_ucc_organism
from scraper.xml_report import (
    XmlCompra,
    fetch_xml_report,
    parse_xml_report,
)

_UY_TZ = ZoneInfo("America/Montevideo")

if TYPE_CHECKING:
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


def enrich_xml_compra(
    xml_compra: XmlCompra,
    *,
    source_url: str,
) -> CompraEnrichment:
    """Build a :class:`CompraEnrichment` from an :class:`XmlCompra`.

    The organism is resolved through a two-step priority chain:

    1. :func:`scraper.organism_lookup.resolve_organism` with the
       ``(id_inciso, id_ue)`` pair — wins when that pair is mapped.
    2. When the pair is unmapped (the function returned a
       ``"Desconocido ..."`` placeholder) AND ``id_ucc`` is present,
       :func:`scraper.ucc_lookup.resolve_ucc_organism` supplies the
       organism name from the UCC codiguera.

    When both lookups miss, the final value is the inciso/ue
    ``"Desconocido ({i}-{u})"`` fallback. The ``license_link`` is
    built deterministically from ``id_compra``.
    """

    organism = resolve_organism(xml_compra.id_inciso, xml_compra.id_ue)
    if organism.startswith(UNKNOWN_ORGANISM_PREFIX) and xml_compra.id_ucc is not None:
        organism = resolve_ucc_organism(xml_compra.id_ucc)
    return CompraEnrichment(
        organism=organism,
        license_link=build_license_link(xml_compra.id_compra),
        source_url=source_url,
    )


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
# The dict projections and ``bulk_insert`` live in
# :mod:`scraper.persistence` — imported at the top. They were
# historically duplicated between this module and
# ``scripts/scrape_day_by_day.py``; the canonical home is the
# persistence module, imported by both call sites.

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _run_scrape_for_day(
    *,
    target_day: date,
    source_a_base_url: str,
    session: Session,
    bcu_client: BcuClient,
    client: httpx.Client,
    start_hour: int,
    end_hour: int,
) -> list[CompraRow]:
    """Run the full XML-only pipeline for a single day.

    The pipeline is:

    1. Fetch the day-scoped XML report (Source A).
    2. Parse it into :class:`XmlCompra` records (each carrying
       nested :class:`XmlAdjudicacion` and :class:`XmlOferente`
       children).
    3. Enrich each compra via :func:`enrich_xml_compra` — the
       organism is resolved through
       :func:`scraper.organism_lookup.resolve_organism` and the
       ``license_link`` is built deterministically from
       ``id_compra``. No RSS fetch, no per-compra fallback, no join.
    4. Normalize per :class:`XmlAdjudicacion` (BCU rate fetch +
       currency conversion). The per-row ``amount_uyu`` lives on
       the child :class:`AdjudicacionRow`.
    5. Return the :class:`CompraRow` records for batch flush by
       the caller.

    The shared ``client`` is the long-lived ``httpx.Client`` constructed
    by :func:`run_scrape` — passing it in keeps the connection pool warm
    across every fetch in the run.

    Returns the list of :class:`CompraRow` for this day, or ``[]``
    when the day produced no data (empty XML, HTTP error, or no
    surviving normalization). Persistence is the caller's
    responsibility — :func:`run_scrape` accumulates these into a buffer
    and flushes periodically to amortize commit overhead.
    """

    log = logging.getLogger("scraper.run_scrape")

    url_a = build_source_a_url(
        source_a_base_url,
        target_day,
        start_hour,
        end_hour,
    )

    # ------------------------------------------------------------------
    # 1. Fetch the XML report
    # ------------------------------------------------------------------
    try:
        xml_text = fetch_xml_report(url_a, client=client)
    except httpx.HTTPError as exc:
        log.error("XML fetch failed for %s: %s; skipping day", target_day, exc)
        return []

    # ------------------------------------------------------------------
    # 2. Parse
    # ------------------------------------------------------------------
    xml_compras: list[XmlCompra] = list(parse_xml_report(xml_text))
    total_adjs = sum(len(c.adjudicaciones) for c in xml_compras)
    log.info(
        "Parsed %d compras (%d adjudicaciones) for %s",
        len(xml_compras),
        total_adjs,
        target_day,
    )

    if not xml_compras:
        log.info("No XML records for %s; nothing to insert", target_day)
        return []

    # ------------------------------------------------------------------
    # 3. Enrich — resolve organism + build license_link per compra
    # ------------------------------------------------------------------
    enriched: list[tuple[XmlCompra, CompraEnrichment]] = [
        (compra, enrich_xml_compra(compra, source_url=url_a)) for compra in xml_compras
    ]

    # ------------------------------------------------------------------
    # 4. Normalize (BCU rate fetch + currency conversion per row)
    # ------------------------------------------------------------------
    normalized: list[CompraRow] = []
    for compra, enrichment in enriched:
        try:
            normalized.append(normalize_compra(compra, enrichment, bcu_client))
        except (BcuError, NormalizationError) as exc:
            log.warning(
                "Normalization failed for id_compra=%s on %s: %s",
                compra.id_compra,
                target_day,
                exc,
            )

    if not normalized:
        log.info(
            "No records survived normalization for %s; nothing to insert", target_day
        )
        return []

    # ------------------------------------------------------------------
    # 5. Return CompraRow records for batch flush by run_scrape
    # ------------------------------------------------------------------
    log.info("Normalized %d compras for %s", len(normalized), target_day)
    return normalized


def run_scrape(
    *,
    session: Session | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    start_hour: int = 0,
    end_hour: int = 23,
    flush_size: int = 1000,
    flush_interval: int = 7,
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
    flush_size:
        Flush the insert buffer when it reaches this many records.
        Defaults to ``1000``. The 7-day / 1000-record defaults cap
        crash data loss at ~700 records.
    flush_interval:
        Flush the insert buffer every N days with data, regardless of
        record count. Defaults to ``7``. Whichever threshold fires
        first triggers the flush.

    Returns
    -------
    int
        The total number of CompraRow records passed to ``ON CONFLICT
        DO NOTHING`` across all days in the range. Existing rows are
        silently skipped by the database.

    Notes
    -----
    The :class:`BcuClient` is constructed once per call and shared across
    every day in the range so its in-memory rate cache is reused.

    ``run_scrape`` accumulates normalized rows in memory and flushes when
    either ``flush_size`` records or ``flush_interval`` data-bearing days
    is reached. Any remaining rows are drained during cleanup/finally
    handling before the HTTP client and an owned SQLAlchemy session are
    closed. The cleanup drain bounds graceful/exception-path loss to the
    pending buffer; it does not protect against violent process
    termination. Database errors remain fail-hard and the original run
    exception is preserved if a secondary cleanup drain fails. The
    ``ON CONFLICT DO NOTHING`` clause in :func:`bulk_insert` keeps a
    re-run after a crash mid-batch idempotent.
    """

    settings = get_settings()
    log = logging.getLogger("scraper.run_scrape")

    # ------------------------------------------------------------------
    # Resolve date range
    # ------------------------------------------------------------------
    if start_date is None and end_date is None:
        start_date = end_date = datetime.now(_UY_TZ).date()
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

    # Shared ``httpx.Client`` for the whole run. Connection pooling +
    # keep-alive so every XML and BCU request reuses pooled TCP
    # connections instead of paying per-fetch handshake cost. The client
    # is thread-safe, so any future parallel phase (currently the
    # pipeline is sequential) can share it safely. Conservative pool
    # (20 max / 10 keepalive) keeps the load on the government server
    # polite.
    client = httpx.Client(
        timeout=30.0,
        limits=httpx.Limits(
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=30,
        ),
    )

    total_inserted = 0
    buffer: list[CompraRow] = []
    days_since_flush = 0
    db: Session = cast("Session", session)
    # ``completed_normally`` distinguishes a drain that runs after a
    # successful day loop from one that runs while an earlier exception
    # is propagating. ``bcu_client`` is initialized here so cleanup can
    # close it safely even when its construction fails.
    completed_normally = False
    bcu_client: BcuClient | None = None
    try:
        bcu_client = BcuClient(settings.bcu_api_url, client=client)
        current = start_date
        while current <= end_date:
            normalized = _run_scrape_for_day(
                target_day=current,
                source_a_base_url=settings.source_a_base_url,
                session=db,
                bcu_client=bcu_client,
                client=client,
                start_hour=start_hour,
                end_hour=end_hour,
            )
            if normalized:
                buffer.extend(normalized)
                days_since_flush += 1

            if buffer and (
                len(buffer) >= flush_size or days_since_flush >= flush_interval
            ):
                try:
                    inserted = bulk_insert(db, buffer)
                    total_inserted += inserted
                    log.info(
                        "Flushed %d records (%d total)",
                        inserted,
                        total_inserted,
                    )
                    buffer.clear()
                    days_since_flush = 0
                except SQLAlchemyError as exc:
                    log.error("Database insert failed: %s", exc)
                    db.rollback()
                    raise

            current += timedelta(days=1)

        completed_normally = True
    finally:
        # Drain any pending rows BEFORE closing the shared HTTP client
        # and the owned session. On the exception path this bounds loss
        # to the pending buffer while the original run exception wins.
        try:
            if buffer:
                try:
                    inserted = bulk_insert(db, buffer)
                    total_inserted += inserted
                    log.info(
                        "%s: %d records (%d total)",
                        (
                            "Final cleanup flush"
                            if completed_normally
                            else "Error-path cleanup flush"
                        ),
                        inserted,
                        total_inserted,
                    )
                    buffer.clear()
                    days_since_flush = 0
                except Exception as cleanup_exc:
                    # Exception precedence: roll back a failed drain, but
                    # never let the cleanup failure mask the run outcome.
                    if isinstance(cleanup_exc, SQLAlchemyError):
                        try:
                            db.rollback()
                        except Exception as rollback_exc:
                            log.error(
                                "Secondary rollback failure during cleanup drain: %s",
                                rollback_exc,
                            )
                    if completed_normally:
                        # Fail-hard: never report success after a DB error.
                        raise
                    log.error(
                        "Final buffer drain failed during error-path cleanup; "
                        "preserving original run exception",
                        exc_info=True,
                    )
        finally:
            if bcu_client is not None:
                bcu_client.close()
            client.close()
            if owns_session and session is not None:
                session.close()

    log.info(
        "Scraper run complete: %d records submitted to DB across %s..%s",
        total_inserted,
        start_date,
        end_date,
    )
    return total_inserted


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
