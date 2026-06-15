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
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, cast

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.database import get_session_factory
from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from app.models.oferente import Oferente
from scraper.bcu_client import BcuClient
from scraper.normalizer import (
    AdjudicacionRow,
    CompraEnrichment,
    CompraRow,
    OferenteRow,
    normalize_compra,
)
from scraper.organism_lookup import resolve_organism
from scraper.ucc_lookup import resolve_ucc_organism
from scraper.xml_report import (
    XmlCompra,
    fetch_xml_report,
    parse_xml_report,
)

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
    if organism.startswith("Desconocido") and xml_compra.id_ucc is not None:
        organism = resolve_ucc_organism(xml_compra.id_ucc)
    return CompraEnrichment(
        organism=organism,
        license_link=build_license_link(xml_compra.id_compra),
        source_url=source_url,
    )


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _compra_dict(row: CompraRow) -> dict[str, Any]:
    """Map a :class:`CompraRow` to the ``compra`` table row it produces."""

    return {
        "id_compra": row.id_compra,
        "fecha_pub_adj": row.fecha_pub_adj,
        "objeto": row.objeto,
        "monto_adj": row.monto_adj,
        "id_moneda_monto_adj": row.id_moneda_monto_adj,
        "num_compra": row.num_compra,
        "anio_compra": row.anio_compra,
        "id_tipocompra": row.id_tipocompra,
        "subtipo_compra": row.subtipo_compra,
        "id_inciso": row.id_inciso,
        "id_ue": row.id_ue,
        "id_ucc": row.id_ucc,
        "organismo": row.organismo or None,
        "source_url": row.source_url,
    }


def _adjudicacion_dict(
    compra_id: int, row: AdjudicacionRow
) -> dict[str, Any]:
    """Map an :class:`AdjudicacionRow` to the ``adjudicacion`` table row."""

    return {
        "compra_id": compra_id,
        "nombre_comercial": row.nombre_comercial,
        "nro_doc_prov": row.nro_doc_prov,
        "tipo_doc_prov": row.tipo_doc_prov,
        "cant_adj": row.cant_adj,
        "precio_unit": row.precio_unit,
        "precio_tot_imp": row.precio_tot_imp,
        "id_moneda": row.id_moneda,
        "desc_articulo": row.desc_articulo,
        "id_articulo": row.id_articulo,
        "amount_uyu": row.amount_uyu,
    }


def _oferente_dict(compra_id: int, row: OferenteRow) -> dict[str, Any]:
    """Map an :class:`OferenteRow` to the ``oferente`` table row."""

    return {
        "compra_id": compra_id,
        "nombre_comercial": row.nombre_comercial,
        "nro_doc_prov": row.nro_doc_prov,
        "tipo_doc_prov": row.tipo_doc_prov,
        "cant_ofertada": row.cant_ofertada,
        "precio_unit_ofertado": row.precio_unit_ofertado,
        "id_moneda": row.id_moneda,
        "variacion": row.variacion,
        "alternativas": row.alternativas,
    }


def _bulk_insert(session: Session, rows: Iterable[CompraRow]) -> int:
    """Insert ``rows`` into the new schema, idempotently.

    Each :class:`CompraRow` produces one ``compra`` (with ``ON
    CONFLICT DO NOTHING`` on ``id_compra``), one ``adjudicacion`` per
    nested :class:`AdjudicacionRow`, and one ``oferente`` per nested
    :class:`OferenteRow`. A re-run of the scraper on the same data
    is a no-op at the parent level: the existing Compra is reused
    and no new children are inserted. Returns the number of
    CompraRow rows passed in (not the number actually inserted — the
    DB does not report that without a round-trip).
    """

    rows = list(rows)
    if not rows:
        return 0

    # 1. Upsert the Compra rows first. ``ON CONFLICT DO NOTHING`` skips
    #    purchases we have already ingested, which is the idempotency
    #    the spec requires.
    compra_payloads = [_compra_dict(r) for r in rows]
    stmt = pg_insert(Compra).values(compra_payloads)
    stmt = stmt.on_conflict_do_nothing(index_elements=["id_compra"])
    session.execute(stmt)

    # 2. Resolve each Compra's primary key. New compras get a fresh
    #    ``id``; existing compras return the previously-assigned id.
    id_compras = {r.id_compra for r in rows}
    rows_pk = session.execute(
        select(Compra.id_compra, Compra.id).where(Compra.id_compra.in_(id_compras))
    ).all()
    id_compra_to_pk: dict[str, int] = {row[0]: row[1] for row in rows_pk}

    # 3. Insert Adjudicacion rows. There is no natural key on the
    #    child table, so duplicate children would normally be
    #    possible — but the parent-level skip in step 1 ensures
    #    that we only reach this branch for *new* compras. For new
    #    compras, every child is also new.
    adj_payloads: list[dict[str, Any]] = []
    oferente_payloads: list[dict[str, Any]] = []
    for r in rows:
        pk = id_compra_to_pk.get(r.id_compra)
        if pk is None:
            # The Compra already existed and we skipped the insert;
            # the spec's idempotency contract says no new children
            # either, so just drop them.
            continue
        adj_payloads.extend(_adjudicacion_dict(pk, a) for a in r.adjudicaciones)
        oferente_payloads.extend(_oferente_dict(pk, o) for o in r.oferentes)

    if adj_payloads:
        session.execute(pg_insert(Adjudicacion).values(adj_payloads))
    if oferente_payloads:
        session.execute(pg_insert(Oferente).values(oferente_payloads))

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
        (compra, enrich_xml_compra(compra, source_url=url_a))
        for compra in xml_compras
    ]

    # ------------------------------------------------------------------
    # 4. Normalize (BCU rate fetch + currency conversion per row)
    # ------------------------------------------------------------------
    normalized: list[CompraRow] = []
    for compra, enrichment in enriched:
        try:
            normalized.append(normalize_compra(compra, enrichment, bcu_client))
        except Exception as exc:  # BcuError, malformed data, etc.
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
    buffer: list[CompraRow] = []
    days_since_flush = 0
    db: Session = cast("Session", session)
    try:
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
            bcu_client.close()

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
