"""Tests for :func:`app.services.catalog.catalog_date_span`.

The span is the oldest and newest adjudication publication date in the whole
catalogue. It backs two things: the ``<lastmod>`` of the index URL in
sitemap.xml, and the ``temporalCoverage`` published on the home page's
``Dataset`` node. Both must describe the same reality, so both read this one
query.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from app.services.catalog import catalog_date_span

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _seed_compra(
    db_session: Session,
    id_compra: str,
    fecha_pub_adj: date,
    *,
    with_award: bool = True,
) -> None:
    """Insert a Compra, optionally with one adjudication attached."""

    compra = Compra(
        id_compra=id_compra,
        fecha_pub_adj=fecha_pub_adj,
        id_tipocompra="CD",
        organismo="ANEP",
    )
    db_session.add(compra)
    db_session.flush()

    if not with_award:
        return

    db_session.add(
        Adjudicacion(
            compra_id=compra.id,
            nombre_comercial="Empresa SA",
            nro_doc_prov="210000000018",
            tipo_doc_prov="RUT",
            cant_adj=Decimal("1"),
            precio_tot_imp=Decimal("100"),
            desc_articulo="Item",
            id_moneda=0,
            amount_uyu=Decimal("100"),
        )
    )
    db_session.flush()


def test_span_is_none_pair_on_empty_database(db_session: Session) -> None:
    """An empty catalogue reports no span rather than a fabricated one."""

    assert catalog_date_span(db_session) == (None, None)


def test_span_covers_the_oldest_and_newest_publication(db_session: Session) -> None:
    """The span brackets every publication date in the catalogue."""

    _seed_compra(db_session, "c-1", date(2021, 3, 4))
    _seed_compra(db_session, "c-2", date(2026, 9, 13))
    _seed_compra(db_session, "c-3", date(2023, 11, 30))

    assert catalog_date_span(db_session) == (date(2021, 3, 4), date(2026, 9, 13))


def test_span_ignores_compras_without_adjudications(db_session: Session) -> None:
    """A parent-only compra is outside the span the site can display.

    Every figure the site publishes comes from the adjudicacion join, so a compra
    whose lines were all skipped is invisible. Letting it widen the span would
    publish a coverage claim the catalogue cannot substantiate.
    """

    _seed_compra(db_session, "c-with-award", date(2021, 3, 4))
    _seed_compra(db_session, "c-parent-only-old", date(1999, 1, 1), with_award=False)
    _seed_compra(db_session, "c-parent-only-new", date(2030, 1, 1), with_award=False)

    assert catalog_date_span(db_session) == (date(2021, 3, 4), date(2021, 3, 4))


def test_span_returns_a_single_date_twice_when_only_one_published(
    db_session: Session,
) -> None:
    """One publication makes the span a zero-length interval, not a half-open one."""

    _seed_compra(db_session, "c-only", date(2024, 6, 1))

    assert catalog_date_span(db_session) == (date(2024, 6, 1), date(2024, 6, 1))
