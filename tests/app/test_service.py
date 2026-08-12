"""Unit tests for the CSV export service helpers.

Tests for ``iter_adjudications`` — the generator that yields
:class:`AdjudicationRow` objects for the streaming CSV export.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from app.services.filters import AdjudicationFilters
from app.services.listing import AdjudicationRow, iter_adjudications

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

CURRENT_YEAR = date.today().year


# ---------------------------------------------------------------------------
# iter_adjudications — generator
# ---------------------------------------------------------------------------


def test_iter_adjudications_returns_matching_rows(
    db_session: Session, make_adjudication
) -> None:
    """iter_adjudications yields AdjudicationRow objects matching the filter."""

    make_adjudication(
        winning_company="ITER-COMPANY-A",
        organism="ITER-ORG-OSE",
        date=date(CURRENT_YEAR, 3, 1),
    )
    make_adjudication(
        winning_company="ITER-COMPANY-B",
        organism="ITER-ORG-MIN",
        date=date(CURRENT_YEAR, 4, 1),
    )

    filters = AdjudicationFilters(organism="OSE")
    rows = list(iter_adjudications(db_session, filters))

    assert len(rows) == 1
    assert isinstance(rows[0], AdjudicationRow)
    assert rows[0].winning_company == "ITER-COMPANY-A"
    assert rows[0].organism == "ITER-ORG-OSE"


def test_iter_adjudications_returns_empty_for_no_match(
    db_session: Session, make_adjudication
) -> None:
    """iter_adjudications yields nothing when no rows match the filter."""

    make_adjudication(
        winning_company="EMPTY-CHECK",
        organism="EMPTY-ORG",
        date=date(CURRENT_YEAR, 3, 1),
    )

    filters = AdjudicationFilters(organism="NONEXISTENT-ORG")
    rows = list(iter_adjudications(db_session, filters))

    assert rows == []


def test_iter_adjudications_orders_newest_first(
    db_session: Session, make_adjudication
) -> None:
    """iter_adjudications yields rows newest-first (date DESC)."""

    make_adjudication(
        winning_company="ORDER-OLD",
        date=date(2024, 1, 1),
    )
    make_adjudication(
        winning_company="ORDER-NEW",
        date=date(2024, 6, 1),
    )

    filters = AdjudicationFilters(
        date_from=date(2024, 1, 1),
        date_to=date(2024, 12, 31),
    )
    rows = list(iter_adjudications(db_session, filters))

    assert len(rows) == 2
    assert rows[0].winning_company == "ORDER-NEW"
    assert rows[1].winning_company == "ORDER-OLD"


def test_iter_adjudications_preserves_raw_values(
    db_session: Session, make_adjudication
) -> None:
    """iter_adjudications yields raw Decimal/date values (not formatted)."""

    make_adjudication(
        winning_company="RAW-CO",
        amount=Decimal("1234567.89"),
        amount_uyu=Decimal("1234567.89"),
        date=date(2024, 3, 15),
    )

    filters = AdjudicationFilters(
        date_from=date(2024, 1, 1),
        date_to=date(2024, 12, 31),
    )
    rows = list(iter_adjudications(db_session, filters))

    assert len(rows) == 1
    row = rows[0]
    assert row.date == date(2024, 3, 15)
    assert row.amount == Decimal("1234567.89")
    assert row.amount_uyu == Decimal("1234567.89")


def test_iter_adjudications_handles_null_amount_uyu(
    db_session: Session, make_adjudication
) -> None:
    """iter_adjudications yields amount_uyu=None for non-convertible rows."""

    make_adjudication(
        winning_company="NULL-UYU-CO",
        amount_uyu=None,
        date=date(CURRENT_YEAR, 3, 1),
    )

    filters = AdjudicationFilters()
    rows = list(iter_adjudications(db_session, filters))

    assert len(rows) == 1
    assert rows[0].amount_uyu is None
