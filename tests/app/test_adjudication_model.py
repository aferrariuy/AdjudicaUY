"""Unit tests for the SQLAlchemy ORM model.

The tests use the SQLite in-memory engine from ``conftest.py``. They
cover the spec scenarios from ``data-storage``:

* Required-field validation
* Unique-constraint enforcement on (source_url, license_link, date)
* Index presence on the four filter columns

The migration's schema is the source of truth for indexes and
constraints; the model itself only declares what the spec requires.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.models.adjudication import Adjudication


# ---------------------------------------------------------------------------
# Required-field validation
# ---------------------------------------------------------------------------


def test_model_can_be_persisted_with_minimal_valid_data(db_session) -> None:
    """The model accepts the spec's minimum data set."""

    record = Adjudication(
        amount=Decimal("100.00"),
        currency="UYU",
        amount_uyu=Decimal("100.00"),
        winning_company="Acme",
        organism="OSE",
        date=date(2024, 1, 15),
        article="Laptop",
        source_url="https://example.test/xml",
    )
    db_session.add(record)
    db_session.commit()

    assert record.id is not None
    assert record.ingested_at is not None  # server default applied


def test_model_rejects_missing_winning_company(db_session) -> None:
    record = Adjudication(
        amount=Decimal("100.00"),
        currency="UYU",
        organism="OSE",
        date=date(2024, 1, 15),
        article="Laptop",
        source_url="https://example.test/xml",
        winning_company=None,  # NOT NULL column
    )
    db_session.add(record)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_model_rejects_missing_amount(db_session) -> None:
    record = Adjudication(
        currency="UYU",
        winning_company="Acme",
        organism="OSE",
        date=date(2024, 1, 15),
        article="Laptop",
        source_url="https://example.test/xml",
        amount=None,  # NOT NULL column
    )
    db_session.add(record)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_model_rejects_missing_organism(db_session) -> None:
    record = Adjudication(
        amount=Decimal("100.00"),
        currency="UYU",
        winning_company="Acme",
        organism=None,  # NOT NULL column
        date=date(2024, 1, 15),
        article="Laptop",
        source_url="https://example.test/xml",
    )
    db_session.add(record)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_model_rejects_missing_article(db_session) -> None:
    record = Adjudication(
        amount=Decimal("100.00"),
        currency="UYU",
        winning_company="Acme",
        organism="OSE",
        date=date(2024, 1, 15),
        source_url="https://example.test/xml",
        article=None,  # NOT NULL column
    )
    db_session.add(record)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_model_rejects_missing_date(db_session) -> None:
    record = Adjudication(
        amount=Decimal("100.00"),
        currency="UYU",
        winning_company="Acme",
        organism="OSE",
        article="Laptop",
        source_url="https://example.test/xml",
        date=None,  # NOT NULL column
    )
    db_session.add(record)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_model_rejects_missing_source_url(db_session) -> None:
    record = Adjudication(
        amount=Decimal("100.00"),
        currency="UYU",
        winning_company="Acme",
        organism="OSE",
        date=date(2024, 1, 15),
        article="Laptop",
        source_url=None,  # NOT NULL column
    )
    db_session.add(record)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_model_accepts_null_optional_fields(db_session) -> None:
    """``amount_uyu`` and ``license_link`` MAY be null (non-convertible / RSS gap)."""

    record = Adjudication(
        amount=Decimal("100.00"),
        currency="UIX",
        amount_uyu=None,
        winning_company="Acme",
        organism="OSE",
        date=date(2024, 1, 15),
        article="Servicio",
        license_link=None,
        source_url="https://example.test/xml",
    )
    db_session.add(record)
    db_session.commit()

    assert record.id is not None
    assert record.amount_uyu is None
    assert record.license_link is None


# ---------------------------------------------------------------------------
# Unique constraint
# ---------------------------------------------------------------------------


def test_unique_constraint_blocks_duplicate_records(
    db_session, make_adjudication
) -> None:
    """Re-scraping the same (source_url, license_link, date) is rejected."""

    make_adjudication(
        source_url="https://example.test/xml",
        license_link="https://example.test/licitacion/1",
        date=date(2024, 1, 15),
    )

    duplicate = Adjudication(
        amount=Decimal("9999.00"),
        currency="USD",
        amount_uyu=Decimal("9999.00"),
        winning_company="Otra Empresa",
        organism="Otro Organismo",
        date=date(2024, 1, 15),
        article="Otro articulo",
        source_url="https://example.test/xml",
        license_link="https://example.test/licitacion/1",  # same triple
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_different_source_url_allows_same_license_and_date(
    db_session, make_adjudication
) -> None:
    """The same adjudication from two different sources is two distinct records.

    See ``data-storage`` spec, "Partial duplicate from different source"
    scenario.
    """

    make_adjudication(
        source_url="https://example.test/source-A",
        license_link="https://example.test/licitacion/1",
        date=date(2024, 1, 15),
    )
    other = Adjudication(
        amount=Decimal("100.00"),
        currency="UYU",
        amount_uyu=Decimal("100.00"),
        winning_company="Acme",
        organism="OSE",
        date=date(2024, 1, 15),
        article="Laptop",
        source_url="https://example.test/source-B",  # different source
        license_link="https://example.test/licitacion/1",  # same link, same date
    )
    db_session.add(other)
    db_session.commit()

    assert other.id is not None


def test_different_date_allows_same_source_and_link(
    db_session, make_adjudication
) -> None:
    make_adjudication(
        source_url="https://example.test/xml",
        license_link="https://example.test/licitacion/1",
        date=date(2024, 1, 15),
    )
    second = Adjudication(
        amount=Decimal("100.00"),
        currency="UYU",
        amount_uyu=Decimal("100.00"),
        winning_company="Acme",
        organism="OSE",
        date=date(2024, 1, 16),  # different date
        article="Laptop",
        source_url="https://example.test/xml",
        license_link="https://example.test/licitacion/1",
    )
    db_session.add(second)
    db_session.commit()
    assert second.id is not None


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


def _indexed_column_names(engine) -> set[str]:
    """Return the set of index names defined on the ``adjudications`` table."""

    inspector = inspect(engine)
    indexes = inspector.get_indexes("adjudications")
    names: set[str] = set()
    for idx in indexes:
        if isinstance(idx, dict):
            names.add(idx["name"])
        else:  # pragma: no cover - depends on SA version
            names.add(idx.name)
    return names


def test_index_on_date_exists(engine) -> None:
    names = _indexed_column_names(engine)
    # SQLAlchemy auto-names inline ``index=True`` columns as ``ix_<table>_<col>``.
    assert any("date" in name for name in names)


def test_index_on_winning_company_exists(engine) -> None:
    names = _indexed_column_names(engine)
    assert "ix_company" in names


def test_index_on_organism_exists(engine) -> None:
    names = _indexed_column_names(engine)
    assert "ix_organism" in names


def test_index_on_article_exists(engine) -> None:
    names = _indexed_column_names(engine)
    assert any("article" in name for name in names)


# ---------------------------------------------------------------------------
# Column metadata
# ---------------------------------------------------------------------------


def test_model_columns_have_expected_types() -> None:
    """Sanity-check the column types declared on the model.

    The migration must stay in sync; if a column type changes here the
    migration script is the one that needs updating, not the model.
    """

    columns = {col.name: col for col in Adjudication.__table__.columns}

    assert columns["id"].primary_key is True
    assert not columns["amount"].nullable
    assert not columns["currency"].nullable
    assert columns["amount_uyu"].nullable
    assert not columns["winning_company"].nullable
    assert columns["company_document"].nullable
    assert columns["company_document_type"].nullable
    assert not columns["organism"].nullable
    assert not columns["date"].nullable
    assert columns["license_type"].nullable
    assert not columns["article"].nullable
    assert columns["article_quantity"].nullable
    assert columns["license_link"].nullable
    assert not columns["source_url"].nullable
    assert not columns["ingested_at"].nullable
