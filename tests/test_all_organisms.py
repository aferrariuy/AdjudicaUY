"""Tests for :func:`app.services.catalog.organisms_with_last_activity`.

The query returns every distinct organism name paired with the most recent
adjudication publication date recorded for it, and applies no LIMIT. It feeds
the sitemap.xml route, which needs both the complete unfiltered list of
organism pages and a truthful ``<lastmod>`` for each one.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from app.services.catalog import organisms_with_last_activity

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _seed_compra(
    db_session: Session,
    id_compra: str,
    organismo: str | None,
    fecha_pub_adj: date = date(2024, 6, 1),
) -> Compra:
    """Insert a minimal Compra + Adjudicacion pair for the given organism."""

    compra = Compra(
        id_compra=id_compra,
        fecha_pub_adj=fecha_pub_adj,
        id_tipocompra="CD",
        organismo=organismo,
    )
    db_session.add(compra)
    db_session.flush()

    adj = Adjudicacion(
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
    db_session.add(adj)
    db_session.flush()
    return compra


def test_organisms_returns_distinct_names(db_session: Session) -> None:
    """Duplicate organism names are collapsed into a single entry."""

    _seed_compra(db_session, "c-1", "Ministerio de Interior")
    _seed_compra(db_session, "c-2", "Ministerio de Interior")
    _seed_compra(db_session, "c-3", "ANEP")

    result = organisms_with_last_activity(db_session)
    assert [name for name, _ in result] == ["ANEP", "Ministerio de Interior"]


def test_organisms_last_activity_is_the_newest_publication(db_session: Session) -> None:
    """Each organism carries its own newest publication date, not a global one.

    This is the property the sitemap depends on: a per-URL ``<lastmod>`` that
    reflects when *that* page last gained data. A single shared date would tell
    crawlers every page changed whenever any page did.
    """

    _seed_compra(db_session, "c-1", "ANEP", date(2021, 3, 4))
    _seed_compra(db_session, "c-2", "ANEP", date(2023, 11, 30))
    _seed_compra(db_session, "c-3", "ANEP", date(2022, 1, 1))
    _seed_compra(db_session, "c-4", "BPS", date(2020, 5, 5))

    result = dict(organisms_with_last_activity(db_session))

    assert result == {"ANEP": date(2023, 11, 30), "BPS": date(2020, 5, 5)}


def test_organisms_has_no_limit(db_session: Session) -> None:
    """All distinct organisms are returned, even when there are many."""

    for i in range(250):
        _seed_compra(db_session, f"bulk-{i}", f"Organism {i:04d}")

    result = organisms_with_last_activity(db_session)
    assert len(result) == 250


def test_organisms_returns_empty_list_when_no_data(db_session: Session) -> None:
    """An empty database yields an empty list, not an error."""

    assert organisms_with_last_activity(db_session) == []


def test_organisms_excludes_null_organism(db_session: Session) -> None:
    """Rows with NULL organismo are excluded from the result."""

    _seed_compra(db_session, "c-null", None)
    _seed_compra(db_session, "c-ok", "MSP")

    result = organisms_with_last_activity(db_session)
    assert [name for name, _ in result] == ["MSP"]


def test_organisms_excludes_compras_without_adjudications(db_session: Session) -> None:
    """A parent-only compra cannot make an organism page claim activity.

    Every KPI on the site counts purchases *through* the adjudicacion join, so a
    compra whose lines were all skipped contributes to nothing. The sitemap
    query has to agree, or a page could advertise a ``<lastmod>`` for data it
    does not display.
    """

    _seed_compra(db_session, "c-with-award", "ANEP", date(2021, 3, 4))
    parent_only = Compra(
        id_compra="c-parent-only",
        fecha_pub_adj=date(2025, 12, 31),
        id_tipocompra="CD",
        organismo="ANEP",
    )
    db_session.add(parent_only)
    db_session.flush()

    result = dict(organisms_with_last_activity(db_session))

    assert result == {"ANEP": date(2021, 3, 4)}
