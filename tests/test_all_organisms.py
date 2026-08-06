"""Tests for :func:`app.services.adjudication_service.all_organisms`.

The ``all_organisms`` query returns every distinct organism name in the
database without a LIMIT clause — it feeds the sitemap.xml route, which
needs the complete unfiltered list of organism pages.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from app.services.adjudication_service import all_organisms

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _seed_compra(
    db_session: Session,
    id_compra: str,
    organismo: str,
) -> Compra:
    """Insert a minimal Compra + Adjudicacion pair for the given organism."""

    compra = Compra(
        id_compra=id_compra,
        fecha_pub_adj=date(2024, 6, 1),
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


def test_all_organisms_returns_distinct_names(db_session: Session) -> None:
    """Duplicate organism names are collapsed into a single entry."""

    _seed_compra(db_session, "c-1", "Ministerio de Interior")
    _seed_compra(db_session, "c-2", "Ministerio de Interior")
    _seed_compra(db_session, "c-3", "ANEP")

    result = all_organisms(db_session)
    assert sorted(result) == ["ANEP", "Ministerio de Interior"]


def test_all_organisms_has_no_limit(db_session: Session) -> None:
    """All distinct organisms are returned, even when there are many."""

    for i in range(250):
        _seed_compra(db_session, f"bulk-{i}", f"Organism {i:04d}")

    result = all_organisms(db_session)
    assert len(result) == 250


def test_all_organisms_returns_empty_list_when_no_data(
    db_session: Session,
) -> None:
    """An empty database yields an empty list, not an error."""

    result = all_organisms(db_session)
    assert result == []


def test_all_organisms_excludes_null_organism(db_session: Session) -> None:
    """Rows with NULL organismo are excluded from the result."""

    _seed_compra(db_session, "c-null", None)  # type: ignore[arg-type]
    _seed_compra(db_session, "c-ok", "MSP")

    result = all_organisms(db_session)
    assert result == ["MSP"]
