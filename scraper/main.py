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

Organism enrichment
-------------------
The XML report exposes ``id_inciso`` and ``id_ue`` on every ``<compra>``;
those are mapped to the organism name via the static
:mod:`scraper.organism_lookup` module. ``license_link`` is built
deterministically from ``id_compra`` — no RSS request is issued.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, cast

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.database import get_session_factory
from app.models.adjudication import Adjudication
from scraper.bcu_client import BcuClient
from scraper.normalizer import JoinedRecord, NormalizedRecord, normalize_record
from scraper.organism_lookup import resolve_organism
from scraper.xml_report import XmlAdjudication, fetch_xml_report, parse_xml_report

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


# Deterministic template for the public detail page. The XML report
# carries ``id_compra``; the corresponding detail page is a stable URL
# derived from it. Replacing the RSS-provided link with this template
# removes a network round-trip per record and keeps the link shape
# consistent with what the government actually serves.
_LICENSE_LINK_TEMPLATE = (
    "https://www.comprasestatales.gub.uy/consultas/detalle/id/{id_compra}"
)


def build_license_link(id_compra: str) -> str:
    """Build the public detail-page URL for ``id_compra``.

    Deterministic — no HTTP request — so the same ``id_compra`` always
    produces the same ``license_link`` (see ``organism-lookup`` spec,
    "License Link Construction" scenario).
    """

    return _LICENSE_LINK_TEMPLATE.format(id_compra=id_compra)


def enrich_xml_record(
    xml_record: XmlAdjudication,
    *,
    source_url: str,
) -> JoinedRecord:
    """Build a :class:`JoinedRecord` from an XML record plus the static enrichment.

    The organism is resolved via
    :func:`scraper.organism_lookup.resolve_organism` (warn-on-missing,
    never raises); the ``license_link`` is built deterministically from
    ``id_compra``.
    """

    organism = resolve_organism(xml_record.id_inciso, xml_record.id_ue)
    return JoinedRecord(
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
        organism=organism,
        license_link=build_license_link(xml_record.id_compra),
        source_url=source_url,
        id_articulo=xml_record.id_articulo,
        num_compra=xml_record.num_compra,
        anio_compra=xml_record.anio_compra,
        id_inciso=xml_record.id_inciso,
        id_ue=xml_record.id_ue,
    )


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
        "id_inciso": record.id_inciso,
        "id_ue": record.id_ue,
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
    source_a_base_url: str,
    session: Session,
    bcu_client: BcuClient,
    client: httpx.Client,
    start_hour: int,
    end_hour: int,
) -> list[NormalizedRecord]:
    """Run the full XML-only pipeline for a single day.

    The pipeline is:

    1. Fetch the day-scoped XML report (Source A).
    2. Parse it into :class:`XmlAdjudication` records.
    3. Enrich each record via :func:`enrich_xml_record` — the organism
       is resolved through :func:`scraper.organism_lookup.resolve_organism`
       and the ``license_link`` is built deterministically from
       ``id_compra``. No RSS fetch, no per-compra fallback, no join.
    4. Normalize (BCU rate fetch + currency conversion).
    5. Return the normalized records for batch flush by the caller.

    The shared ``client`` is the long-lived ``httpx.Client`` constructed
    by :func:`run_scrape` — passing it in keeps the connection pool warm
    across every fetch in the run.

    Returns the list of :class:`NormalizedRecord` for this day, or ``[]``
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
    xml_records: list[XmlAdjudication] = list(parse_xml_report(xml_text))
    log.info("Parsed %d XML adjudications for %s", len(xml_records), target_day)

    if not xml_records:
        log.info("No XML records for %s; nothing to insert", target_day)
        return []

    # ------------------------------------------------------------------
    # 3. Enrich — resolve organism + build license_link per record
    # ------------------------------------------------------------------
    # Use the per-day URL as ``source_url`` so the
    # ``(source_url, license_link, date)`` unique constraint dedupes
    # correctly: same day + same link = conflict (skip); different day
    # = different source_url = insert (even if the same adjudication
    # appears on multiple days).
    joined: list[JoinedRecord] = [
        enrich_xml_record(record, source_url=url_a) for record in xml_records
    ]

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
        return []

    # ------------------------------------------------------------------
    # 5. Return normalized records for batch flush by run_scrape
    # ------------------------------------------------------------------
    # Persistence is the orchestrator's responsibility: it accumulates
    # these into a buffer and flushes every ``flush_size`` records or
    # every ``flush_interval`` days, whichever comes first. This shrinks
    # the commit count from O(days) to O(days / flush_interval).
    log.info("Normalized %d records for %s", len(normalized), target_day)
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
        The total number of normalized records passed to ``ON CONFLICT
        DO NOTHING`` across all days in the range. Existing rows are
        silently skipped by the database.

    Notes
    -----
    The :class:`BcuClient` is constructed once per call and shared across
    every day in the range so its in-memory rate cache is reused.

    Persistence is **batched**: per-day records are accumulated into an
    in-memory buffer and committed when either threshold is hit, then a
    final flush drains the buffer in the ``finally`` block. This shrinks
    commit overhead from ``O(days)`` to ``O(days / flush_interval)``.
    The ``ON CONFLICT DO NOTHING`` clause in :func:`_bulk_insert` keeps
    a re-run after a crash mid-batch idempotent.
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
    # Batch insert buffer: instead of committing after every day,
    # accumulate normalized records and flush when either the day count
    # or record count threshold is reached. This shrinks commit overhead
    # from O(days) to O(days / flush_interval); the 7-day / 1000-record
    # defaults cap crash data loss at ~700 records. on_conflict_do_nothing
    # in :func:`_bulk_insert` makes a re-run after a crash mid-batch
    # idempotent.
    buffer: list[NormalizedRecord] = []
    days_since_flush = 0
    # ``session`` is non-None from here on (either the caller's or one
    # borrowed from the factory above). ``cast`` narrows the type for
    # mypy without runtime checks — invariants already enforced by the
    # control flow above.
    db: Session = cast("Session", session)
    try:
        # One BCU client for the entire range — its (bcu_code, date)
        # cache is the whole point of having a long-lived client. We
        # also pass the shared ``httpx.Client`` in so BCU SOAP requests
        # reuse the same connection pool (and so ``BcuClient.close``
        # does NOT close the shared client — see ``owns_client`` below).
        bcu_client = BcuClient(settings.bcu_api_url, client=client)
        try:
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

                # Flush check: whichever threshold (records or days) is
                # reached first triggers a commit. Days-without-data do
                # not advance ``days_since_flush`` — only days that
                # actually contributed records to the buffer count.
                if buffer and (
                    len(buffer) >= flush_size or days_since_flush >= flush_interval
                ):
                    try:
                        inserted = _bulk_insert(db, buffer)
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
        finally:
            # BCU client must not close the shared ``client``; we own
            # its lifecycle below. ``BcuClient.close`` short-circuits
            # when ``owns_client`` is False (we passed one in), so this
            # is a no-op for the shared client.
            bcu_client.close()

        # Final flush: anything left in the buffer must be committed so
        # a graceful exit (or a crash right after) doesn't lose data.
        # ``ON CONFLICT DO NOTHING`` keeps a re-run idempotent in case
        # the process was killed between commit and process exit.
        if buffer:
            try:
                inserted = _bulk_insert(db, buffer)
                total_inserted += inserted
                log.info(
                    "Final flush: %d records (%d total)",
                    inserted,
                    total_inserted,
                )
                buffer.clear()
                days_since_flush = 0
            except SQLAlchemyError as exc:
                log.error("Database insert failed: %s", exc)
                db.rollback()
                raise

        log.info(
            "Scraper run complete: %d records submitted to DB across %s..%s",
            total_inserted,
            start_date,
            end_date,
        )
        return total_inserted

    finally:
        client.close()
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
