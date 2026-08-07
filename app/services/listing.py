"""Adjudication listing, counting, and streaming-query services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from sqlalchemy import func, select

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from app.services.filters import AdjudicationFilters, _apply_filters
from scraper.normalizer import build_license_link, display_currency

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import date
    from decimal import Decimal

    from sqlalchemy.orm import Session


@dataclass(frozen=True)
class AdjudicationRow:
    """Display-shaped view of one adjudicated line item.

    Returned by the service so the Jinja templates can read the same
    field names they used to read off the old ``Adjudication`` ORM
    model. Construction happens inside the service — the web layer
    never builds one by hand.
    """

    date: date
    organism: str
    winning_company: str
    article: str
    amount: Decimal
    currency: str
    amount_uyu: Decimal | None
    license_type: str
    company_document: str | None
    company_document_type: str | None
    license_link: str
    article_id: str | None = None

    @property
    def company_profile_url(self) -> str | None:
        """Return the encoded company profile URL when identity is complete."""

        if not self.company_document_type or not self.company_document:
            return None
        return (
            f"/company/{quote(self.company_document_type, safe='')}/"
            f"{quote(self.company_document, safe='')}"
        )


def list_adjudications(
    session: Session,
    filters: AdjudicationFilters,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[AdjudicationRow]:
    """Return a page of adjudicated line items matching ``filters``, newest first.

    Joins :class:`Compra` and :class:`Adjudicacion`; orders by
    ``Compra.fecha_pub_adj DESC, Adjudicacion.id DESC`` so two line
    items on the same date have a stable order. ``limit`` and ``offset``
    are simple pagination knobs — the route layer may cap them.
    """

    stmt = _listing_query()
    stmt = _apply_filters(stmt, filters)
    stmt = stmt.order_by(Compra.fecha_pub_adj.desc(), Adjudicacion.id.desc())
    stmt = stmt.limit(limit).offset(offset)
    return [_row_to_adjudication_row(row) for row in session.execute(stmt)]


def count_adjudications(session: Session, filters: AdjudicationFilters) -> int:
    """Return the total number of adjudicaciones matching ``filters``.

    Used by the route to render pagination controls and the "showing N
    of M" header. A separate ``COUNT(*)`` query keeps the listing query
    simple.
    """

    stmt = select(func.count(Adjudicacion.id)).join(
        Compra, Compra.id == Adjudicacion.compra_id
    )
    stmt = _apply_filters(stmt, filters)
    return int(session.execute(stmt).scalar_one())


MAX_EXPORT_ROWS: int = 500_000


def iter_adjudications(
    session: Session,
    filters: AdjudicationFilters,
    *,
    chunk_size: int = 1000,
) -> Iterator[AdjudicationRow]:
    """Yield filtered adjudication rows newest-first, using ``_listing_query``.

    The generator uses ``yield_per(chunk_size)`` so the DB driver fetches
    rows in batches instead of loading the entire result set into memory.
    The caller (the route layer) is responsible for closing the session
    when iteration is complete.
    """

    stmt = _listing_query()
    stmt = _apply_filters(stmt, filters)
    stmt = stmt.order_by(Compra.fecha_pub_adj.desc(), Adjudicacion.id.desc())
    stmt = stmt.execution_options(yield_per=chunk_size)
    for row in session.execute(stmt):
        yield _row_to_adjudication_row(row)


def _listing_query() -> Any:
    """Base SELECT for the listing query — selects all display fields.

    Returning a column bundle keeps ``list_adjudications`` focused on
    ordering + limits + filters; this helper centralizes the projection
    so renaming a column only touches one place.
    """

    return select(
        Compra.fecha_pub_adj.label("date"),
        Compra.organismo.label("organism"),
        Compra.id_tipocompra.label("license_type"),
        Compra.id_compra.label("id_compra"),
        Adjudicacion.nombre_comercial.label("winning_company"),
        Adjudicacion.desc_articulo.label("article"),
        Adjudicacion.id_articulo.label("article_id"),
        Adjudicacion.precio_tot_imp.label("amount"),
        Adjudicacion.id_moneda.label("id_moneda"),
        Adjudicacion.amount_uyu.label("amount_uyu"),
        Adjudicacion.nro_doc_prov.label("company_document"),
        Adjudicacion.tipo_doc_prov.label("company_document_type"),
    ).join(Adjudicacion, Adjudicacion.compra_id == Compra.id)


def _row_to_adjudication_row(row: Any) -> AdjudicationRow:
    """Map a SQLAlchemy row to a display-shaped :class:`AdjudicationRow`.

    The ``currency`` field is derived at query time from
    ``id_moneda`` (see :func:`scraper.normalizer.display_currency`); the database does
    not store the display code, and the per-line-item conversion
    tables live in :mod:`scraper.normalizer`. Unknown codes fall back
    to ``"N/D"`` so the template never renders a blank currency.
    """

    id_moneda = getattr(row, "id_moneda", None)
    currency = display_currency(id_moneda) if id_moneda is not None else "N/D"
    organism = row.organism or ""
    license_type = row.license_type or ""
    return AdjudicationRow(
        date=row.date,
        organism=organism,
        winning_company=row.winning_company,
        article=row.article,
        amount=row.amount,
        currency=currency,
        amount_uyu=row.amount_uyu,
        license_type=license_type,
        company_document=row.company_document,
        company_document_type=row.company_document_type,
        license_link=build_license_link(row.id_compra),
        article_id=getattr(row, "article_id", None),
    )
