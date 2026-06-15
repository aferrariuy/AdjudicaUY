"""Unit tests for the new SQLAlchemy models: Compra, Adjudicacion, Oferente.

The tests use the SQLite in-memory engine from ``conftest.py``. They
cover the spec scenarios from ``data-storage``:

* Required-field validation on the natural key
* Unique-constraint enforcement on ``Compra.id_compra``
* Cascade delete from Compra → Adjudicacion
* Cascade delete from Compra → Oferente
* Indexes on the filter columns
* Per-row ``amount_uyu`` nullable for non-convertible currencies
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from app.models.oferente import Oferente
from app.services.adjudication_service import (
    AdjudicationFilters,
    list_adjudications,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_compra_payload(**overrides):
    """Default Compra row payload — tests override the fields they care about."""

    payload = {
        "id_compra": "compra-1",
        "fecha_pub_adj": date(2024, 1, 15),
        "id_tipocompra": "CD",
        "id_inciso": 4,
        "id_ue": 1,
        "organismo": "OSE",
        "source_url": "https://example.test/xml",
    }
    payload.update(overrides)
    return payload


def _build_adjudicacion_payload(**overrides):
    """Default Adjudicacion row payload."""

    payload = {
        "nombre_comercial": "Acme",
        "nro_doc_prov": "210000000001",
        "tipo_doc_prov": "RUT",
        "cant_adj": Decimal("10.00"),
        "precio_tot_imp": Decimal("100.00"),
        "id_moneda": 0,
        "desc_articulo": "Laptop",
        "id_articulo": "42851",
        "amount_uyu": Decimal("100.00"),
    }
    payload.update(overrides)
    return payload


def _build_oferente_payload(**overrides):
    """Default Oferente row payload."""

    payload = {
        "nombre_comercial": "Bidder",
        "nro_doc_prov": "210000000010",
        "tipo_doc_prov": "RUT",
        "cant_ofertada": Decimal("10.00"),
        "precio_unit_ofertado": Decimal("80.00"),
        "id_moneda": 0,
        "variacion": None,
        "alternativas": None,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Compra — required fields
# ---------------------------------------------------------------------------


def test_compra_persists_with_minimal_valid_data(db_session) -> None:
    """Compra accepts the spec's minimum data set (id_compra + fecha_pub_adj)."""

    compra = Compra(**_build_compra_payload())
    db_session.add(compra)
    db_session.commit()

    assert compra.id is not None
    assert compra.ingested_at is not None  # server default applied


def test_compra_rejects_missing_id_compra(db_session) -> None:
    """id_compra is the natural key — NULL must be rejected."""

    payload = _build_compra_payload()
    payload["id_compra"] = None
    compra = Compra(**payload)
    db_session.add(compra)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_compra_rejects_missing_fecha_pub_adj(db_session) -> None:
    """fecha_pub_adj is NOT NULL — must be present for every compra."""

    payload = _build_compra_payload()
    payload["fecha_pub_adj"] = None
    compra = Compra(**payload)
    db_session.add(compra)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_compra_accepts_null_optional_fields(db_session) -> None:
    """Every other Compra column is nullable (data-storage spec, "Compra Table")."""

    payload = _build_compra_payload(
        objeto=None,
        monto_adj=None,
        id_moneda_monto_adj=None,
        num_compra=None,
        anio_compra=None,
        id_tipocompra=None,
        subtipo_compra=None,
        id_inciso=None,
        id_ue=None,
        organismo=None,
        source_url=None,
    )
    compra = Compra(**payload)
    db_session.add(compra)
    db_session.commit()

    assert compra.id is not None
    assert compra.objeto is None
    assert compra.organismo is None


# ---------------------------------------------------------------------------
# Compra — unique constraint
# ---------------------------------------------------------------------------


def test_unique_constraint_blocks_duplicate_id_compra(db_session) -> None:
    """Re-scraping the same id_compra is rejected by the unique constraint."""

    db_session.add(Compra(**_build_compra_payload(id_compra="dup-1")))
    db_session.commit()

    duplicate = Compra(**_build_compra_payload(id_compra="dup-1"))
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# Compra — indexes (data-storage spec, "Indexes for filter columns")
# ---------------------------------------------------------------------------


def _indexed_column_names(engine, table_name: str) -> set[str]:
    """Return the set of index column names on a given table."""

    inspector = inspect(engine)
    indexes = inspector.get_indexes(table_name)
    names: set[str] = set()
    for idx in indexes:
        if isinstance(idx, dict):
            names.add(idx["name"])
        else:  # pragma: no cover - depends on SA version
            names.add(idx.name)
    return names


def test_compra_index_on_fecha_pub_adj_exists(engine) -> None:
    assert "ix_compra_fecha_pub_adj" in _indexed_column_names(engine, "compra")


def test_compra_index_on_id_inciso_exists(engine) -> None:
    assert "ix_compra_id_inciso" in _indexed_column_names(engine, "compra")


def test_compra_index_on_id_ue_exists(engine) -> None:
    assert "ix_compra_id_ue" in _indexed_column_names(engine, "compra")


def test_compra_index_on_id_tipocompra_exists(engine) -> None:
    assert "ix_compra_id_tipocompra" in _indexed_column_names(engine, "compra")


def test_compra_unique_index_on_id_compra_exists(engine) -> None:
    """The natural key is enforced via a UNIQUE index, not just a constraint."""

    assert "ix_compra_id_compra" in _indexed_column_names(engine, "compra")


# ---------------------------------------------------------------------------
# Adjudicacion
# ---------------------------------------------------------------------------


def test_adjudicacion_persists_with_minimal_valid_data(db_session) -> None:
    """Adjudicacion requires a Compra parent and a nombre_comercial."""

    compra = Compra(**_build_compra_payload())
    db_session.add(compra)
    db_session.flush()

    adj = Adjudicacion(
        compra_id=compra.id,
        **_build_adjudicacion_payload(),
    )
    db_session.add(adj)
    db_session.commit()

    assert adj.id is not None


def test_adjudicacion_rejects_missing_nombre_comercial(db_session) -> None:
    compra = Compra(**_build_compra_payload())
    db_session.add(compra)
    db_session.flush()

    payload = _build_adjudicacion_payload()
    payload["nombre_comercial"] = None
    adj = Adjudicacion(compra_id=compra.id, **payload)
    db_session.add(adj)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_adjudicacion_rejects_missing_precio_tot_imp(db_session) -> None:
    """precio_tot_imp is NOT NULL — the line-item amount is mandatory."""

    compra = Compra(**_build_compra_payload())
    db_session.add(compra)
    db_session.flush()

    payload = _build_adjudicacion_payload()
    payload["precio_tot_imp"] = None
    adj = Adjudicacion(compra_id=compra.id, **payload)
    db_session.add(adj)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_adjudicacion_accepts_null_amount_uyu(db_session) -> None:
    """amount_uyu is NULL for non-convertible currencies."""

    compra = Compra(**_build_compra_payload())
    db_session.add(compra)
    db_session.flush()

    adj = Adjudicacion(
        compra_id=compra.id,
        **_build_adjudicacion_payload(amount_uyu=None),
    )
    db_session.add(adj)
    db_session.commit()

    assert adj.amount_uyu is None


def test_adjudicacion_cascade_delete_with_parent(db_session) -> None:
    """Dropping a Compra removes its child Adjudicaciones (ON DELETE CASCADE)."""

    compra = Compra(**_build_compra_payload())
    db_session.add(compra)
    db_session.flush()

    adj = Adjudicacion(compra_id=compra.id, **_build_adjudicacion_payload())
    db_session.add(adj)
    db_session.commit()

    db_session.delete(compra)
    db_session.commit()

    remaining = (
        db_session.query(Adjudicacion)
        .filter(Adjudicacion.compra_id == compra.id)
        .all()
    )
    assert remaining == []


def test_adjudicacion_index_on_compra_id_exists(engine) -> None:
    assert "ix_adjudicacion_compra_id" in _indexed_column_names(engine, "adjudicacion")


def test_adjudicacion_index_on_id_articulo_exists(engine) -> None:
    assert "ix_adjudicacion_id_articulo" in _indexed_column_names(
        engine, "adjudicacion"
    )


# ---------------------------------------------------------------------------
# Oferente
# ---------------------------------------------------------------------------


def test_oferente_persists_with_minimal_valid_data(db_session) -> None:
    """Oferente requires only a Compra parent — every other column is nullable."""

    compra = Compra(**_build_compra_payload())
    db_session.add(compra)
    db_session.flush()

    of = Oferente(compra_id=compra.id)
    db_session.add(of)
    db_session.commit()

    assert of.id is not None
    assert of.nombre_comercial is None


def test_multiple_oferentes_per_compra(db_session) -> None:
    """A single Compra can have many Oferentes
    (data-storage spec, "Multiple oferentes")."""

    compra = Compra(**_build_compra_payload())
    db_session.add(compra)
    db_session.flush()

    for i in range(3):
        of = Oferente(
            compra_id=compra.id,
            **_build_oferente_payload(nombre_comercial=f"Bidder {i}"),
        )
        db_session.add(of)
    db_session.commit()

    oferentes = (
        db_session.query(Oferente).filter(Oferente.compra_id == compra.id).all()
    )
    assert len(oferentes) == 3
    assert {o.nombre_comercial for o in oferentes} == {
        "Bidder 0",
        "Bidder 1",
        "Bidder 2",
    }


def test_compra_with_no_oferentes_ok(db_session) -> None:
    """A Compra with no Oferentes is valid
    (data-storage spec, "Compra with no oferentes")."""

    compra = Compra(**_build_compra_payload())
    db_session.add(compra)
    db_session.commit()

    oferentes = (
        db_session.query(Oferente).filter(Oferente.compra_id == compra.id).all()
    )
    assert oferentes == []


def test_oferente_cascade_delete_with_parent(db_session) -> None:
    """Dropping a Compra removes its child Oferentes (ON DELETE CASCADE)."""

    compra = Compra(**_build_compra_payload())
    db_session.add(compra)
    db_session.flush()

    of = Oferente(compra_id=compra.id, **_build_oferente_payload())
    db_session.add(of)
    db_session.commit()

    db_session.delete(compra)
    db_session.commit()

    remaining = (
        db_session.query(Oferente).filter(Oferente.compra_id == compra.id).all()
    )
    assert remaining == []


def test_oferente_index_on_compra_id_exists(engine) -> None:
    assert "ix_oferente_compra_id" in _indexed_column_names(engine, "oferente")


# ---------------------------------------------------------------------------
# Idempotency at the service layer
# ---------------------------------------------------------------------------


def test_filter_by_article_id_single_value(db_session, make_adjudication) -> None:
    """A single article_id filter MUST return only matching rows."""

    make_adjudication(id_articulo="42851")
    make_adjudication(id_articulo="42852")
    make_adjudication(id_articulo=None)

    rows = list_adjudications(db_session, AdjudicationFilters(article_id="42851"))

    assert len(rows) == 1
    assert rows[0].article_id == "42851"


def test_filter_by_article_id_comma_separated(db_session, make_adjudication) -> None:
    """A comma-separated list MUST match any of the IDs (IN set predicate)."""

    make_adjudication(id_articulo="42851")
    make_adjudication(id_articulo="42852")
    make_adjudication(id_articulo="42853")
    make_adjudication(id_articulo=None)

    rows = list_adjudications(
        db_session, AdjudicationFilters(article_id="42851, 42852")
    )

    returned = sorted(row.article_id for row in rows)
    assert returned == sorted(["42851", "42852"])


def test_filter_by_article_id_excludes_nulls(db_session, make_adjudication) -> None:
    """Rows with NULL id_articulo MUST NOT match the IN-set filter."""

    make_adjudication(id_articulo="42851")
    make_adjudication(id_articulo=None)

    rows = list_adjudications(db_session, AdjudicationFilters(article_id="42851"))
    assert len(rows) == 1


def test_filter_by_article_id_ignores_empty_entries(
    db_session, make_adjudication
) -> None:
    """Trailing/empty comma entries MUST be ignored — no empty IN crash."""

    make_adjudication(id_articulo="42851")
    make_adjudication(id_articulo="42852")

    rows = list_adjudications(
        db_session, AdjudicationFilters(article_id=" 42851 , , ")
    )
    assert len(rows) == 1
    assert rows[0].article_id == "42851"
