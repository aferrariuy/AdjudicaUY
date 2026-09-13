"""Unfiltered catalog queries used for sitemap discovery.

Everything here is uncapped and unfiltered: the sitemap has to enumerate the
whole catalogue, so it cannot reuse the filter-aware, limit-aware aggregates the
dashboard uses.

Each row carries the newest publication date recorded for its page. That date is
what makes a truthful ``<lastmod>`` possible: a crawler uses ``<lastmod>`` to
schedule its next visit, so a single catalogue-wide date would tell it that every
one of the ~28.7k pages changed whenever any page did, which is worse than
publishing no date at all. Being wrong in that direction is not free either —
Google only trusts ``<lastmod>`` while it stays "consistently and verifiably
accurate".

The date is read *through* the ``adjudicacion`` join on purpose. A compra whose
lines were all skipped contributes to no figure the site displays, so it must not
make a page advertise a modification it cannot show.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.orm import Session


def organisms_with_last_activity(session: Session) -> list[tuple[str, date]]:
    """Return every distinct organism name with its newest publication date.

    Used by the sitemap.xml route to enumerate all publicly crawlable organism
    pages and to date each one. Unlike
    :func:`~app.services.dashboard.distinct_organisms` (which filters and caps at
    ``limit`` for the datalist), this query returns the complete unfiltered set so
    the sitemap stays current as new organisms appear. Organisms with no
    resolvable name are omitted: they have no page to point at.
    """

    stmt = (
        select(Compra.organismo, func.max(Compra.fecha_pub_adj))
        .select_from(Compra)
        .join(Adjudicacion, Adjudicacion.compra_id == Compra.id)
        .where(Compra.organismo.is_not(None))
        .group_by(Compra.organismo)
        .order_by(Compra.organismo.asc())
    )
    return [(row[0], row[1]) for row in session.execute(stmt)]


def companies_with_last_activity(session: Session) -> list[tuple[str, str, date]]:
    """Return every crawlable provider identity with its newest publication date.

    A provider page is identified by its document type *and* number, so both
    segments must be present and non-blank; a row missing either cannot identify
    a crawlable profile. Rows without both segments remain available to ordinary
    name-based listings.
    """

    company_type = Adjudicacion.tipo_doc_prov
    company_number = Adjudicacion.nro_doc_prov
    stmt = (
        select(company_type, company_number, func.max(Compra.fecha_pub_adj))
        .select_from(Adjudicacion)
        .join(Compra, Compra.id == Adjudicacion.compra_id)
        .where(
            company_type.is_not(None),
            company_number.is_not(None),
            func.trim(company_type) != "",
            func.trim(company_number) != "",
        )
        .group_by(company_type, company_number)
        .order_by(company_type.asc(), company_number.asc())
    )
    return [(row[0], row[1], row[2]) for row in session.execute(stmt)]


def entities_active_between(
    session: Session, *, start_date: date, end_date: date
) -> tuple[list[str], list[tuple[str, str]]]:
    """Return the organisms and companies that published inside a date window.

    After a scrape the worker needs to know which pages changed, and the insert
    path cannot tell it: ``bulk_insert`` uses ``ON CONFLICT DO NOTHING`` and
    reports nothing about which rows it skipped. Deriving the set from the stored
    data instead describes what the site now *shows* rather than what the worker
    attempted. Re-announcing a URL that did not actually change is harmless:
    IndexNow treats a submission as a hint to recrawl, and the sitemap carries the
    authoritative per-page dates.

    Both bounds are inclusive, matching the scraper's own range semantics. Only
    entities with a crawlable identity are returned, because there is no URL to
    submit for the others — the sitemap leaves them out for the same reason.
    """

    organisms_stmt = (
        select(Compra.organismo)
        .select_from(Compra)
        .join(Adjudicacion, Adjudicacion.compra_id == Compra.id)
        .where(
            Compra.organismo.is_not(None),
            Compra.fecha_pub_adj >= start_date,
            Compra.fecha_pub_adj <= end_date,
        )
        .distinct()
        .order_by(Compra.organismo.asc())
    )

    company_type = Adjudicacion.tipo_doc_prov
    company_number = Adjudicacion.nro_doc_prov
    companies_stmt = (
        select(company_type, company_number)
        .select_from(Adjudicacion)
        .join(Compra, Compra.id == Adjudicacion.compra_id)
        .where(
            company_type.is_not(None),
            company_number.is_not(None),
            func.trim(company_type) != "",
            func.trim(company_number) != "",
            Compra.fecha_pub_adj >= start_date,
            Compra.fecha_pub_adj <= end_date,
        )
        .distinct()
        .order_by(company_type.asc(), company_number.asc())
    )

    organisms = [row[0] for row in session.execute(organisms_stmt)]
    companies = [(row[0], row[1]) for row in session.execute(companies_stmt)]
    return organisms, companies


def catalog_date_span(session: Session) -> tuple[date | None, date | None]:
    """Return the oldest and newest publication dates in the catalogue.

    The one query behind both the index URL's ``<lastmod>`` in sitemap.xml and
    the ``temporalCoverage`` published on the home page's ``Dataset`` node, so
    the two can never disagree. ``(None, None)`` on an empty catalogue: an
    unmeasured span must stay unasserted rather than be guessed.
    """

    stmt = (
        select(func.min(Compra.fecha_pub_adj), func.max(Compra.fecha_pub_adj))
        .select_from(Compra)
        .join(Adjudicacion, Adjudicacion.compra_id == Compra.id)
    )
    row = session.execute(stmt).one()
    return (row[0], row[1])
