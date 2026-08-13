"""Persistence layer for the scraper pipeline.

The ``compra`` / ``adjudicacion`` / ``oferente`` insert path lives
here. The module is the single source of truth for projecting a
:class:`CompraRow` batch onto the new schema and committing it
idempotently.

Design notes
------------
* The dict projections (:func:`_compra_dict`,
  :func:`_adjudicacion_dict`, :func:`_oferente_dict`) are pure
  functions — they have no DB access, just a 1:1 field mapping.
  Pure functions are trivially testable and survive the
  ``scraper.main`` / ``scripts/scrape_day_by_day.py`` split that
  the rest of the pipeline shares.
* :func:`bulk_insert` uses ``ON CONFLICT DO NOTHING`` on
  ``compra.id_compra`` so a re-run of the scraper on the same
  data is a no-op at the parent level. The function lets
  :class:`~sqlalchemy.exc.SQLAlchemyError` propagate (fail-hard)
  so the orchestrating cron / Dokploy job can surface DB errors.
  Soft-failure callers (``scripts/scrape_day_by_day.py``) wrap
  the call in their own try/except.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from app.models.oferente import Oferente

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.orm import Session

    from scraper.normalizer import (
        AdjudicacionRow,
        CompraRow,
        OferenteRow,
    )


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


def _adjudicacion_dict(compra_id: int, row: AdjudicacionRow) -> dict[str, Any]:
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


def bulk_insert(session: Session, rows: Iterable[CompraRow]) -> int:
    """Insert ``rows`` into the new schema, idempotently.

    Each :class:`CompraRow` produces one ``compra`` (with ``ON
    CONFLICT DO NOTHING`` on ``id_compra``), one ``adjudicacion`` per
    nested :class:`AdjudicacionRow`, and one ``oferente`` per nested
    :class:`OferenteRow`. A re-run of the scraper on the same data
    is a no-op at the parent level: the existing Compra is reused
    and no new children are inserted. Returns the number of
    CompraRow rows passed in (not the number actually inserted — the
    DB does not report that without a round-trip).

    Errors from :class:`~sqlalchemy.exc.SQLAlchemyError` are NOT
    caught here — the canonical worker relies on the orchestrating
    cron / Dokploy job seeing the failure. Soft-failure callers
    (the day-by-day script) wrap this call in their own try/except.
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

    # 3. Insert Adjudicacion and Oferente rows.
    #    ON CONFLICT DO NOTHING ensures idempotency on re-runs:
    #    the unique constraints on the child tables
    #    (compra_id, nombre_comercial, desc_articulo) and
    #    (compra_id, nombre_comercial) absorb the duplicate
    #    payloads from a second scrape of the same data.
    adj_payloads: list[dict[str, Any]] = []
    oferente_payloads: list[dict[str, Any]] = []
    for r in rows:
        pk = id_compra_to_pk.get(r.id_compra)
        if pk is None:
            raise RuntimeError(
                f"no database id resolved for compra {r.id_compra!r} after upsert"
            )
        adj_payloads.extend(_adjudicacion_dict(pk, a) for a in r.adjudicaciones)
        oferente_payloads.extend(_oferente_dict(pk, o) for o in r.oferentes)

    if adj_payloads:
        session.execute(
            pg_insert(Adjudicacion)
            .values(adj_payloads)
            .on_conflict_do_nothing(
                index_elements=["compra_id", "nombre_comercial", "desc_articulo"]
            )
        )
    if oferente_payloads:
        session.execute(
            pg_insert(Oferente)
            .values(oferente_payloads)
            .on_conflict_do_nothing(index_elements=["compra_id", "nombre_comercial"])
        )

    session.commit()
    return len(rows)
