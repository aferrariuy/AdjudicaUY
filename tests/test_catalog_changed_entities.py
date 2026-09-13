"""Tests for :func:`app.services.catalog.entities_active_between`.

After a successful scrape the worker knows *when* it scraped but not which rows
the database actually accepted — ``bulk_insert`` uses ``ON CONFLICT DO NOTHING``
and the database does not report which rows it skipped. So the changed pages are
derived from the data instead: the entities that published inside the scraped
window. That has the useful property of describing what the site now *shows*,
rather than what the worker *attempted*.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from app.services.catalog import entities_active_between

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _seed(
    db_session: Session,
    id_compra: str,
    fecha_pub_adj: date,
    *,
    organismo: str | None = "ANEP",
    company_type: str | None = "RUT",
    company_number: str | None = "210000000012",
    with_award: bool = True,
) -> None:
    """Insert a Compra, optionally with one award carrying the given identity."""

    compra = Compra(
        id_compra=id_compra,
        fecha_pub_adj=fecha_pub_adj,
        id_tipocompra="CD",
        organismo=organismo,
    )
    db_session.add(compra)
    db_session.flush()

    if not with_award:
        return

    db_session.add(
        Adjudicacion(
            compra_id=compra.id,
            nombre_comercial="Empresa SA",
            nro_doc_prov=company_number,
            tipo_doc_prov=company_type,
            cant_adj=Decimal("1"),
            precio_tot_imp=Decimal("100"),
            desc_articulo="Item",
            id_moneda=0,
            amount_uyu=Decimal("100"),
        )
    )
    db_session.flush()


def test_returns_only_entities_active_inside_the_window(db_session: Session) -> None:
    """The scraped day's entities are returned; other days' are not."""

    _seed(db_session, "c-in", date(2026, 9, 13), organismo="ANEP")
    _seed(
        db_session,
        "c-in-2",
        date(2026, 9, 13),
        organismo="BPS",
        company_number="210000000099",
    )
    _seed(db_session, "c-out", date(2026, 9, 12), organismo="MSP")

    organisms, companies = entities_active_between(
        db_session, start_date=date(2026, 9, 13), end_date=date(2026, 9, 13)
    )

    assert organisms == ["ANEP", "BPS"]
    assert companies == [("RUT", "210000000012"), ("RUT", "210000000099")]


def test_window_bounds_are_inclusive(db_session: Session) -> None:
    """Both ends of the window count as inside it.

    The scraper's own range is inclusive at both ends, so a page updated on an
    edge day must not be dropped from the notification.
    """

    _seed(db_session, "c-first", date(2026, 9, 1), organismo="ANEP")
    _seed(db_session, "c-last", date(2026, 9, 30), organismo="BPS")
    _seed(db_session, "c-before", date(2026, 8, 31), organismo="MSP")
    _seed(db_session, "c-after", date(2026, 10, 1), organismo="MTSS")

    organisms, _companies = entities_active_between(
        db_session, start_date=date(2026, 9, 1), end_date=date(2026, 9, 30)
    )

    assert organisms == ["ANEP", "BPS"]


def test_ignores_compras_without_adjudications(db_session: Session) -> None:
    """A parent-only compra changed no page the site can show.

    Every page reads through the adjudicacion join, so notifying a crawler about
    a compra with no visible awards would send it to a page that did not change.
    """

    _seed(db_session, "c-parent-only", date(2026, 9, 13), with_award=False)

    organisms, companies = entities_active_between(
        db_session, start_date=date(2026, 9, 13), end_date=date(2026, 9, 13)
    )

    assert organisms == []
    assert companies == []


def test_omits_entities_without_a_crawlable_identity(db_session: Session) -> None:
    """A missing organism or document pair removes only that half.

    There is no URL to submit for an entity with no name or no document identity,
    which is why the sitemap leaves them out too. The two halves are independent:
    a row missing its organism can still identify a crawlable company, and vice
    versa. Blank document segments are treated as missing, not as a document.
    """

    _seed(
        db_session,
        "c-no-organism",
        date(2026, 9, 13),
        organismo=None,
        company_number="210000000777",
    )
    _seed(
        db_session,
        "c-no-doc",
        date(2026, 9, 13),
        organismo="ANEP",
        company_type=None,
        company_number=None,
    )
    _seed(
        db_session,
        "c-blank-doc",
        date(2026, 9, 13),
        organismo="MTSS",
        company_type="",
        company_number="",
    )

    organisms, companies = entities_active_between(
        db_session, start_date=date(2026, 9, 13), end_date=date(2026, 9, 13)
    )

    assert organisms == ["ANEP", "MTSS"]
    assert companies == [("RUT", "210000000777")]


def test_returns_empty_pair_when_nothing_published(db_session: Session) -> None:
    """A run that stored nothing notifiable reports no URLs."""

    assert entities_active_between(
        db_session, start_date=date(2026, 9, 13), end_date=date(2026, 9, 13)
    ) == ([], [])
