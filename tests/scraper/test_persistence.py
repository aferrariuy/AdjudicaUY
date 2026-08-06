"""Unit tests for :mod:`scraper.persistence`.

The persistence module is the single source of truth for the
``compra`` / ``adjudicacion`` / ``oferente`` insert path. It
provides:

* :func:`_compra_dict` — project a :class:`CompraRow` onto the
  ``compra`` table row.
* :func:`_adjudicacion_dict` — project an :class:`AdjudicacionRow`
  onto the ``adjudicacion`` table row (carries the parent FK).
* :func:`_oferente_dict` — project an :class:`OferenteRow` onto
  the ``oferente`` table row (carries the parent FK).
* :func:`_bulk_insert` — insert a batch of :class:`CompraRow`
  records, idempotently on ``compra.id_compra`` (re-runs of the
  scraper on the same data are a no-op at the parent level).

These tests describe the contract for the extracted module and
act as approval tests for the DRY refactor that moved the
functions from ``scraper.main`` and ``scripts/scrape_day_by_day.py``
into a single home.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy import func, select

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from app.models.oferente import Oferente
from scraper.normalizer import (
    AdjudicacionRow,
    CompraRow,
    OferenteRow,
)
from scraper.persistence import (
    _adjudicacion_dict,
    _bulk_insert,
    _compra_dict,
    _oferente_dict,
)

# ---------------------------------------------------------------------------
# _compra_dict
# ---------------------------------------------------------------------------


def _compra_row(
    *,
    id_compra: str = "1319278",
    id_ucc: int | None = None,
    organismo: str = "Desconocido",
) -> CompraRow:
    """Build a minimal :class:`CompraRow` for dict-projection tests."""

    return CompraRow(
        id_compra=id_compra,
        fecha_pub_adj=date(2024, 1, 15),
        id_tipocompra="CD",
        id_moneda_monto_adj=0,
        objeto="Adquisición",
        monto_adj=Decimal("1000.00"),
        num_compra="86825",
        anio_compra="2024",
        subtipo_compra=None,
        id_inciso=4,
        id_ue=1,
        id_ucc=id_ucc,
        organismo=organismo,
        license_link="https://www.comprasestatales.gub.uy/consultas/detalle/id/1319278",
        source_url="https://example.test/xml",
        adjudicaciones=[],
        oferentes=[],
    )


def test_compra_dict_includes_all_columns() -> None:
    """``_compra_dict`` carries every column the ``compra`` table needs.

    This is the "shape" test — it pins the full payload so a future
    schema addition that forgets to update the projection breaks the
    test, not the production code.
    """

    row = _compra_row(id_ucc=54, organismo="Ministerio del Interior")

    payload: dict[str, Any] = _compra_dict(row)

    assert payload == {
        "id_compra": "1319278",
        "fecha_pub_adj": date(2024, 1, 15),
        "objeto": "Adquisición",
        "monto_adj": Decimal("1000.00"),
        "id_moneda_monto_adj": 0,
        "num_compra": "86825",
        "anio_compra": "2024",
        "id_tipocompra": "CD",
        "subtipo_compra": None,
        "id_inciso": 4,
        "id_ue": 1,
        "id_ucc": 54,
        "organismo": "Ministerio del Interior",
        "source_url": "https://example.test/xml",
    }


def test_compra_dict_includes_id_ucc_when_none() -> None:
    """``_compra_dict`` MUST carry ``id_ucc`` as ``None`` when absent."""

    row = _compra_row(id_ucc=None)

    payload: dict[str, Any] = _compra_dict(row)

    assert "id_ucc" in payload
    assert payload["id_ucc"] is None


def test_compra_dict_normalizes_empty_organismo_to_none() -> None:
    """``_compra_dict`` MUST convert an empty ``organismo`` string to ``None``.

    The ``compra.organismo`` column is nullable; the table rejects
    empty strings in some flows. The projection coerces ``""`` to
    ``None`` so the database stores NULL, not the empty string.
    """

    row = _compra_row(organismo="")

    payload: dict[str, Any] = _compra_dict(row)

    assert payload["organismo"] is None


# ---------------------------------------------------------------------------
# _adjudicacion_dict
# ---------------------------------------------------------------------------


def _adjudicacion_row(
    *,
    nombre_comercial: str = "Empresa SA",
    id_moneda: int = 0,
    amount_uyu: Decimal | None = Decimal("1000.00"),
) -> AdjudicacionRow:
    """Build a minimal :class:`AdjudicacionRow` for dict-projection tests."""

    return AdjudicacionRow(
        id_compra="1319278",
        nombre_comercial=nombre_comercial,
        nro_doc_prov="210000000018",
        tipo_doc_prov="RUT",
        cant_adj=Decimal("10.00"),
        precio_unit=Decimal("100.00"),
        precio_tot_imp=Decimal("1000.00"),
        id_moneda=id_moneda,
        currency="UYU",
        amount_uyu=amount_uyu,
        desc_articulo="Laptop Dell Latitude",
        id_articulo="42851",
    )


def test_adjudicacion_dict_carries_compra_id() -> None:
    """``_adjudicacion_dict`` MUST embed the parent ``compra_id`` FK."""

    row = _adjudicacion_row()

    payload: dict[str, Any] = _adjudicacion_dict(42, row)

    assert payload["compra_id"] == 42


def test_adjudicacion_dict_carries_all_columns() -> None:
    """``_adjudicacion_dict`` carries every column the child table needs.

    Shape test — the full payload is pinned so a schema addition
    that forgets to update the projection fails here.
    """

    row = _adjudicacion_row()

    payload: dict[str, Any] = _adjudicacion_dict(7, row)

    assert payload == {
        "compra_id": 7,
        "nombre_comercial": "Empresa SA",
        "nro_doc_prov": "210000000018",
        "tipo_doc_prov": "RUT",
        "cant_adj": Decimal("10.00"),
        "precio_unit": Decimal("100.00"),
        "precio_tot_imp": Decimal("1000.00"),
        "id_moneda": 0,
        "desc_articulo": "Laptop Dell Latitude",
        "id_articulo": "42851",
        "amount_uyu": Decimal("1000.00"),
    }


def test_adjudicacion_dict_amount_uyu_can_be_none() -> None:
    """``_adjudicacion_dict`` MUST pass through a NULL ``amount_uyu``.

    Non-convertible currencies (UI, UR, OHR, ...) store NULL there
    — the projection must NOT coerce it to 0 or any default.
    """

    row = _adjudicacion_row(amount_uyu=None)

    payload: dict[str, Any] = _adjudicacion_dict(7, row)

    assert "amount_uyu" in payload
    assert payload["amount_uyu"] is None


# ---------------------------------------------------------------------------
# _oferente_dict
# ---------------------------------------------------------------------------


def _oferente_row(
    *,
    nombre_comercial: str | None = "Bidder SA",
    id_moneda: int | None = 0,
) -> OferenteRow:
    """Build a minimal :class:`OferenteRow` for dict-projection tests."""

    return OferenteRow(
        id_compra="1319278",
        nombre_comercial=nombre_comercial,
        nro_doc_prov="210000000050",
        tipo_doc_prov="RUT",
        cant_ofertada=Decimal("10.00"),
        precio_unit_ofertado=Decimal("80.00"),
        id_moneda=id_moneda,
        variacion=None,
        alternativas=None,
    )


def test_oferente_dict_carries_compra_id() -> None:
    """``_oferente_dict`` MUST embed the parent ``compra_id`` FK."""

    row = _oferente_row()

    payload: dict[str, Any] = _oferente_dict(99, row)

    assert payload["compra_id"] == 99


def test_oferente_dict_carries_all_columns() -> None:
    """``_oferente_dict`` carries every column the child table needs."""

    row = _oferente_row()

    payload: dict[str, Any] = _oferente_dict(11, row)

    assert payload == {
        "compra_id": 11,
        "nombre_comercial": "Bidder SA",
        "nro_doc_prov": "210000000050",
        "tipo_doc_prov": "RUT",
        "cant_ofertada": Decimal("10.00"),
        "precio_unit_ofertado": Decimal("80.00"),
        "id_moneda": 0,
        "variacion": None,
        "alternativas": None,
    }


def test_oferente_dict_handles_optional_nones() -> None:
    """``_oferente_dict`` MUST pass through NULL optional columns.

    Oferente columns are nullable (bidders may have no document,
    no currency, no alternatives). The projection must NOT coerce
    them to empty strings or zeros.
    """

    row = _oferente_row(nombre_comercial=None, id_moneda=None)

    payload: dict[str, Any] = _oferente_dict(11, row)

    assert payload["nombre_comercial"] is None
    assert payload["id_moneda"] is None


# ---------------------------------------------------------------------------
# _bulk_insert
# ---------------------------------------------------------------------------


def test_bulk_insert_empty_rows_returns_zero(db_session) -> None:
    """``_bulk_insert`` on an empty list is a no-op (returns 0)."""

    result = _bulk_insert(db_session, [])

    assert result == 0
    # No rows were inserted
    count = db_session.scalar(select(func.count()).select_from(Compra))
    assert count == 0


def test_bulk_insert_persists_one_compra_with_children(db_session) -> None:
    """``_bulk_insert`` writes the compra, the adjud, and the oferente.

    End-to-end check: the parent lands, the children attach to the
    parent's primary key, and the function returns the number of
    CompraRow records passed in.
    """

    compra = _compra_row(id_compra="BULK-1", organismo="Test Org")
    compra.adjudicaciones.append(_adjudicacion_row())
    compra.oferentes.append(_oferente_row())

    result = _bulk_insert(db_session, [compra])

    assert result == 1

    # Parent
    stored_compra = db_session.scalar(
        select(Compra).where(Compra.id_compra == "BULK-1")
    )
    assert stored_compra is not None
    assert stored_compra.organismo == "Test Org"

    # Child: one adjud and one oferente attached to the same FK
    adjs = db_session.scalars(
        select(Adjudicacion).where(Adjudicacion.compra_id == stored_compra.id)
    ).all()
    assert len(adjs) == 1
    assert adjs[0].nombre_comercial == "Empresa SA"

    oferentes = db_session.scalars(
        select(Oferente).where(Oferente.compra_id == stored_compra.id)
    ).all()
    assert len(oferentes) == 1
    assert oferentes[0].nombre_comercial == "Bidder SA"


def test_bulk_insert_parent_idempotent_on_id_compra(db_session) -> None:
    """Re-running ``_bulk_insert`` on the same data inserts nothing.

    The parent ``compra`` AND its child ``adjudicacion`` /
    ``oferente`` rows are all guarded by unique constraints with
    ``ON CONFLICT DO NOTHING`` — a second call with the same
    data MUST NOT add a second row of any kind.
    """

    # First call uses 3 different adjudicaciones and 2 different oferentes
    # so the child counts are non-trivial (3 and 2). The second call MUST
    # leave the counts unchanged.
    adj1 = _adjudicacion_row(nombre_comercial="Winner A", id_moneda=0)
    adj2 = _adjudicacion_row(nombre_comercial="Winner B", id_moneda=0)
    adj3 = _adjudicacion_row(nombre_comercial="Winner C", id_moneda=0)
    ofe1 = _oferente_row(nombre_comercial="Bidder X")
    ofe2 = _oferente_row(nombre_comercial="Bidder Y")

    compra = _compra_row(id_compra="BULK-DUP", organismo="First")
    compra.adjudicaciones.extend([adj1, adj2, adj3])
    compra.oferentes.extend([ofe1, ofe2])

    first = _bulk_insert(db_session, [compra])
    second = _bulk_insert(db_session, [compra])

    assert first == 1
    assert second == 1  # Returns input length, not actually-inserted count

    # Parent-level idempotency holds.
    compras = db_session.scalars(
        select(Compra).where(Compra.id_compra == "BULK-DUP")
    ).all()
    assert len(compras) == 1

    # Child-level idempotency holds — no duplicates from a re-run.
    assert db_session.scalar(select(func.count()).select_from(Adjudicacion)) == 3
    assert db_session.scalar(select(func.count()).select_from(Oferente)) == 2


def test_bulk_insert_child_idempotent_on_rerun(db_session) -> None:
    """A re-run with the same CompraRow does not duplicate its children.

    Focused regression test for the child-duplication bug: even a
    single Compra with 2 adjudicaciones and 1 oferente must keep
    its child counts stable across a second ``_bulk_insert`` call.
    """

    compra = _compra_row(id_compra="BULK-CHILD")
    compra.adjudicaciones.append(_adjudicacion_row())
    compra.adjudicaciones.append(
        _adjudicacion_row(nombre_comercial="Other Winner", id_moneda=0)
    )
    compra.oferentes.append(_oferente_row())

    _bulk_insert(db_session, [compra])
    _bulk_insert(db_session, [compra])

    # Exactly the children from one Compra — no duplicates.
    assert db_session.scalar(select(func.count()).select_from(Adjudicacion)) == 2
    assert db_session.scalar(select(func.count()).select_from(Oferente)) == 1


def test_bulk_insert_persists_multiple_compras(db_session) -> None:
    """``_bulk_insert`` handles a batch of N compras in one call.

    Verifies the loop body, the FK resolution, and the return
    value (input length, not inserted count).
    """

    rows = [
        _compra_row(id_compra=f"BULK-MULTI-{i}", organismo=f"Org {i}") for i in range(3)
    ]
    for r in rows:
        r.adjudicaciones.append(_adjudicacion_row())
        r.oferentes.append(_oferente_row())

    result = _bulk_insert(db_session, rows)

    assert result == 3

    count = db_session.scalar(select(func.count()).select_from(Compra))
    assert count == 3


def test_bulk_insert_propagates_sqlalchemy_error(db_session) -> None:
    """``_bulk_insert`` lets ``SQLAlchemyError`` propagate (fail-hard).

    The canonical worker relies on this — a DB error must surface
    so the orchestrating cron / Dokploy job can see it. The script
    ``scripts/scrape_day_by_day.py`` wraps the call in its own
    try/except to preserve soft-failure behavior.
    """

    from sqlalchemy.exc import SQLAlchemyError

    class _AlwaysFailSession:
        """Stand-in session whose ``execute`` always raises."""

        def execute(self, *_args: Any, **_kwargs: Any) -> None:
            raise SQLAlchemyError("simulated DB failure")

        def commit(self) -> None:  # pragma: no cover - never reached
            pass

    with pytest.raises(SQLAlchemyError, match="simulated DB failure"):
        _bulk_insert(cast("Session", _AlwaysFailSession()), [_compra_row()])
