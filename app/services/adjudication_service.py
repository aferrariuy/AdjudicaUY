"""Application services for querying adjudications from the database.

This module is the only place the web layer talks to SQLAlchemy. Routes
call functions like :func:`list_adjudications` or :func:`ranking_by_company`
with plain Python values; the service builds the SQLAlchemy query, applies
active filters with AND logic, executes it, and returns plain data
structures (lists of ``Adjudication`` rows, ``(label, value)`` pairs for
charts, etc.).

Keeping the query construction in one place means:

* Routes stay thin and free of SQLAlchemy specifics.
* Filter parsing and validation live next to the query they affect.
* Tests can drive the service directly with an in-memory SQLite session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import Column, and_, func, select

from app.models.adjudication import Adjudication

if TYPE_CHECKING:
    from decimal import Decimal

    from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Filter value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdjudicationFilters:
    """Immutable bundle of active filters passed from the route layer.

    Each attribute corresponds to one form field on the index page. An
    attribute that is ``None`` (or empty string) means "no filter on this
    column" — the service MUST NOT apply the filter in that case. The
    service is responsible for translating non-``None`` values into the
    appropriate SQLAlchemy predicate.
    """

    company: str | None = None
    organism: str | None = None
    article: str | None = None
    article_id: str | None = None
    date_from: date | None = None
    date_to: date | None = None

    def has_any(self) -> bool:
        """Return ``True`` when at least one filter attribute is active."""

        return any(
            getattr(self, field) not in (None, "")
            for field in (
                "company",
                "organism",
                "article",
                "article_id",
                "date_from",
                "date_to",
            )
        )


def _normalize(text: str | None) -> str | None:
    """Strip and collapse a user-typed string, returning ``None`` if empty."""

    if text is None:
        return None
    stripped = text.strip()
    return stripped or None


def filters_from_query_params(params: dict[str, str | None]) -> AdjudicationFilters:
    """Build an :class:`AdjudicationFilters` from raw query parameters.

    Empty strings and missing keys are normalized to ``None`` so the
    service layer can treat them uniformly. Date strings that cannot be
    parsed as ISO 8601 (``YYYY-MM-DD``) are also normalized to ``None`` —
    the route should validate inputs upstream, but the service stays
    defensive.
    """

    def _maybe_date(value: str | None) -> date | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return date.fromisoformat(stripped)
        except ValueError:
            return None

    return AdjudicationFilters(
        company=_normalize(params.get("company")),
        organism=_normalize(params.get("organism")),
        article=_normalize(params.get("article")),
        article_id=_normalize(params.get("article_id")),
        date_from=_maybe_date(params.get("date_from")),
        date_to=_maybe_date(params.get("date_to")),
    )


# ---------------------------------------------------------------------------
# Predicate construction
# ---------------------------------------------------------------------------


def _is_organism_predicate(predicate: Any) -> bool:
    """Return ``True`` if ``predicate`` is the ILIKE filter on ``organism``.

    Used by :func:`distinct_organisms` to drop the organism filter from
    the predicate list — that way the dropdown can suggest names from
    the remaining dimensions even when an organism is already selected.
    """
    if not hasattr(predicate, "left") or not hasattr(predicate, "operator"):
        return False
    left = predicate.left
    operator_name = getattr(predicate.operator, "__name__", "")
    return (
        isinstance(left, Column)
        and left.key == "organism"
        and operator_name == "ilike_op"
    )


def _build_predicates(filters: AdjudicationFilters) -> list[Any]:
    """Translate :class:`AdjudicationFilters` into a list of SQLAlchemy predicates.

    The list is always AND-combined when applied to a ``select()`` via
    :func:`sqlalchemy.and_`. Text fields use ``ILIKE`` (case-insensitive
    partial match) — see the filtering-ui spec, "Filter with article
    text" / "Filter with winning company" scenarios. Date fields are
    inclusive on both ends.
    """

    predicates: list[Any] = []

    if filters.company:
        predicates.append(Adjudication.winning_company.ilike(f"%{filters.company}%"))
    if filters.organism:
        predicates.append(Adjudication.organism.ilike(f"%{filters.organism}%"))
    if filters.article:
        predicates.append(Adjudication.article.ilike(f"%{filters.article}%"))
    if filters.article_id:
        # Comma-separated list of exact IDs → IN set predicate. Whitespace
        # and empty entries are dropped so trailing commas ("1234, ") do
        # not pollute the lookup. NULLs are excluded from IN by SQL
        # semantics, matching the spec.
        ids = [piece.strip() for piece in filters.article_id.split(",")]
        ids = [piece for piece in ids if piece]
        if ids:
            predicates.append(Adjudication.article_id.in_(ids))
    if filters.date_from is not None:
        predicates.append(Adjudication.date >= filters.date_from)
    if filters.date_to is not None:
        predicates.append(Adjudication.date <= filters.date_to)

    return predicates


def _apply_filters(stmt: Any, filters: AdjudicationFilters) -> Any:
    """Apply the predicates from :func:`_build_predicates` to ``stmt``."""

    predicates = _build_predicates(filters)
    if predicates:
        stmt = stmt.where(and_(*predicates))
    return stmt


# ---------------------------------------------------------------------------
# Listing + counting
# ---------------------------------------------------------------------------


def list_adjudications(
    session: Session,
    filters: AdjudicationFilters,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[Adjudication]:
    """Return a page of adjudications matching ``filters``, newest first.

    Ordering is ``date DESC, id DESC`` so that two adjudications on the
    same date have a stable order. ``limit`` and ``offset`` are simple
    pagination knobs — the route layer may cap them.
    """

    stmt = select(Adjudication)
    stmt = _apply_filters(stmt, filters)
    stmt = stmt.order_by(Adjudication.date.desc(), Adjudication.id.desc())
    stmt = stmt.limit(limit).offset(offset)
    return list(session.execute(stmt).scalars())


def count_adjudications(session: Session, filters: AdjudicationFilters) -> int:
    """Return the total number of adjudications matching ``filters``.

    Used by the route to render pagination controls and the "showing N
    of M" header. A separate ``COUNT(*)`` query keeps the listing query
    simple.
    """

    stmt = select(func.count(Adjudication.id))
    stmt = _apply_filters(stmt, filters)
    return int(session.execute(stmt).scalar_one())


# ---------------------------------------------------------------------------
# Chart aggregations
# ---------------------------------------------------------------------------


def ranking_by_company(
    session: Session,
    filters: AdjudicationFilters,
    *,
    limit: int = 10,
) -> list[tuple[str, Decimal]]:
    """Return the top companies by total adjudicated amount in UYU.

    The result is a list of ``(company_name, total_amount_uyu)`` pairs,
    sorted descending by amount. ``amount_uyu`` may be ``NULL`` in the
    database (non-convertible currencies); those rows are excluded from
    the ranking so they do not skew the totals.

    The ranking MUST reflect the same filters as the listing (see
    ranking-visualization spec, "Chart reflects active filters"
    scenario).
    """

    stmt = (
        select(
            Adjudication.winning_company.label("company"),
            func.coalesce(func.sum(Adjudication.amount_uyu), 0).label("total"),
        )
        .where(Adjudication.amount_uyu.is_not(None))
        .group_by(Adjudication.winning_company)
        .order_by(func.sum(Adjudication.amount_uyu).desc())
        .limit(limit)
    )
    stmt = _apply_filters(stmt, filters)
    return [(row.company, row.total) for row in session.execute(stmt)]


def _format_month_label(year: int, month: int) -> str:
    """Format ``(year, month)`` as ``YYYY-MM-01`` for the chart X-axis.

    Pulled out so the route layer can also use it (e.g. to fill in
    gaps in the X-axis). Kept private because callers should not need to
    reach in — they receive the formatted label from
    :func:`temporal_by_organism`.
    """

    return f"{int(year):04d}-{int(month):02d}-01"


def temporal_by_organism(
    session: Session,
    filters: AdjudicationFilters,
) -> list[tuple[str, str, Decimal]]:
    """Return monthly totals per organism, ordered by month then organism.

    The result is a flat list of ``(organism, month_label, total)``
    tuples, where ``month_label`` is the first day of the month in ISO
    format (``YYYY-MM-01``). Months with zero adjudications are NOT
    emitted — the chart's X-axis is built from the union of all months
    that actually have data (see temporal-visualization spec, "Monthly
    aggregation" scenario).

    Implementation note: ``EXTRACT(year|month FROM d)`` is the only
    month-truncation primitive that is portable across PostgreSQL and
    SQLite (the test suite uses SQLite). The ISO label is assembled in
    Python so the SQL stays portable; PostgreSQL's ``date_trunc`` and
    SQLite's ``strftime('%Y-%m-01', d)`` would also work but only on
    one dialect each.
    """

    year_col = func.extract("year", Adjudication.date).label("year")
    month_col = func.extract("month", Adjudication.date).label("month")
    stmt = (
        select(
            Adjudication.organism.label("organism"),
            year_col,
            month_col,
            func.coalesce(func.sum(Adjudication.amount_uyu), 0).label("total"),
        )
        .where(Adjudication.amount_uyu.is_not(None))
        .group_by(Adjudication.organism, year_col, month_col)
        .order_by(year_col.asc(), month_col.asc(), Adjudication.organism.asc())
    )
    stmt = _apply_filters(stmt, filters)
    return [
        (row.organism, _format_month_label(row.year, row.month), row.total)
        for row in session.execute(stmt)
    ]


# ---------------------------------------------------------------------------
# Filter value sources
# ---------------------------------------------------------------------------


def distinct_organisms(
    session: Session,
    filters: AdjudicationFilters,
    *,
    limit: int = 200,
) -> list[str]:
    """Return distinct organism names that match the *other* active filters.

    Used to populate the organism filter as a ``<datalist>`` so users see
    names already in the database. Excluding the organism filter itself
    from the predicate set lets the user discover new organisms
    incrementally: typing "Min" surfaces every "Ministerio …" regardless
    of which one was last selected.

    Distinct names are returned in alphabetical order, capped at
    ``limit`` to keep the datalist small.
    """

    predicates = [
        p for p in _build_predicates(filters) if not _is_organism_predicate(p)
    ]

    stmt = (
        select(Adjudication.organism)
        .distinct()
        .order_by(Adjudication.organism.asc())
        .limit(limit)
    )
    if predicates:
        stmt = stmt.where(and_(*predicates))
    return [row[0] for row in session.execute(stmt)]


__all__ = [
    "AdjudicationFilters",
    "count_adjudications",
    "distinct_organisms",
    "filters_from_query_params",
    "list_adjudications",
    "ranking_by_company",
    "temporal_by_organism",
]
