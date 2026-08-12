"""Tests for company identities used by sitemap discovery."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from app.services.catalog import all_companies

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _seed_company(
    db_session: Session,
    id_compra: str,
    company_type: str | None,
    company_number: str | None,
) -> None:
    """Insert a minimal award row for a provider document identity."""

    compra = Compra(
        id_compra=id_compra,
        fecha_pub_adj=date(2024, 6, 1),
        id_tipocompra="CD",
        organismo="MSP",
    )
    db_session.add(compra)
    db_session.flush()
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


def test_all_companies_returns_distinct_non_null_pairs(
    db_session: Session,
) -> None:
    """Duplicate identities are collapsed and NULL/empty values are omitted."""

    _seed_company(db_session, "c-1", "RUT", "210000000012")
    _seed_company(db_session, "c-2", "RUT", "210000000012")
    _seed_company(db_session, "c-3", "CI", "1234")
    _seed_company(db_session, "c-4", None, "999")
    _seed_company(db_session, "c-5", "RUT", None)
    _seed_company(db_session, "c-6", "", "")

    result = all_companies(db_session)

    assert result == [("CI", "1234"), ("RUT", "210000000012")]


def test_all_companies_returns_empty_for_database_without_document_identities(
    db_session: Session,
) -> None:
    """No crawlable identity is returned when every document segment is empty."""

    _seed_company(db_session, "c-null-type", None, "999")
    _seed_company(db_session, "c-null-number", "RUT", None)
    _seed_company(db_session, "c-empty", "", "")

    result = all_companies(db_session)

    assert result == []
