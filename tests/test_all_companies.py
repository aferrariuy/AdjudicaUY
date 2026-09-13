"""Tests for :func:`app.services.catalog.companies_with_last_activity`.

The query returns every crawlable provider document identity paired with the
newest adjudication publication date recorded for it. It feeds the sitemap.xml
route, which needs the complete unfiltered set of company pages and a truthful
``<lastmod>`` for each one.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from app.services.catalog import companies_with_last_activity

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _seed_company(
    db_session: Session,
    id_compra: str,
    company_type: str | None,
    company_number: str | None,
    fecha_pub_adj: date = date(2024, 6, 1),
) -> None:
    """Insert a minimal award row for a provider document identity."""

    compra = Compra(
        id_compra=id_compra,
        fecha_pub_adj=fecha_pub_adj,
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


def test_companies_returns_distinct_non_null_pairs(db_session: Session) -> None:
    """Duplicate identities are collapsed and NULL/empty values are omitted."""

    _seed_company(db_session, "c-1", "RUT", "210000000012")
    _seed_company(db_session, "c-2", "RUT", "210000000012")
    _seed_company(db_session, "c-3", "CI", "1234")
    _seed_company(db_session, "c-4", None, "999")
    _seed_company(db_session, "c-5", "RUT", None)
    _seed_company(db_session, "c-6", "", "")

    result = companies_with_last_activity(db_session)

    assert [(t, n) for t, n, _ in result] == [
        ("CI", "1234"),
        ("RUT", "210000000012"),
    ]


def test_companies_returns_empty_for_database_without_document_identities(
    db_session: Session,
) -> None:
    """No crawlable identity is returned when every document segment is empty."""

    _seed_company(db_session, "c-null-type", None, "999")
    _seed_company(db_session, "c-null-number", "RUT", None)
    _seed_company(db_session, "c-empty", "", "")

    result = companies_with_last_activity(db_session)

    assert result == []


def test_companies_last_activity_is_the_newest_publication(
    db_session: Session,
) -> None:
    """Each identity carries its own newest publication date."""

    _seed_company(db_session, "c-1", "RUT", "210000000012", date(2021, 3, 4))
    _seed_company(db_session, "c-2", "RUT", "210000000012", date(2023, 11, 30))
    _seed_company(db_session, "c-3", "CI", "1234", date(2020, 5, 5))

    result = companies_with_last_activity(db_session)

    assert result == [
        ("CI", "1234", date(2020, 5, 5)),
        ("RUT", "210000000012", date(2023, 11, 30)),
    ]
