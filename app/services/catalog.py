"""Unfiltered catalog queries used for sitemap discovery."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def all_organisms(session: Session) -> list[str]:
    """Return every distinct organism name in the database, no limit.

    Used by the sitemap.xml route to enumerate all publicly crawlable
    organism pages. Unlike :func:`distinct_organisms` (which filters
    and caps at ``limit`` for the datalist), this query returns the
    complete unfiltered set so the sitemap stays current as new
    organisms appear.
    """

    stmt = (
        select(Compra.organismo)
        .join(Adjudicacion, Adjudicacion.compra_id == Compra.id)
        .distinct()
        .order_by(Compra.organismo.asc())
    )
    return [row[0] for row in session.execute(stmt) if row[0] is not None]


def all_companies(session: Session) -> list[tuple[str, str]]:
    """Return every distinct non-empty provider document identity.

    The sitemap uses this unfiltered discovery query to enumerate company
    profile pages. Rows without both document segments remain available to
    ordinary name-based listings, but cannot identify a crawlable profile.
    """

    company_type = Adjudicacion.tipo_doc_prov
    company_number = Adjudicacion.nro_doc_prov
    stmt = (
        select(company_type, company_number)
        .where(
            company_type.is_not(None),
            company_number.is_not(None),
            func.trim(company_type) != "",
            func.trim(company_number) != "",
        )
        .distinct()
        .order_by(company_type.asc(), company_number.asc())
    )
    return [(row[0], row[1]) for row in session.execute(stmt)]
