"""Dashboard aggregate services for adjudication data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from sqlalchemy import Column, and_, case, func, select

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from app.models.oferente import Oferente
from app.services.filters import (
    AdjudicationFilters,
    _apply_filters,
    _build_predicates,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass(frozen=True)
class KpiSummary:
    """Summary metrics for the currently filtered adjudication set.

    The five values are produced by a single query so the snapshot is
    atomic (no chance of a filter change landing between aggregates)
    and round-trip cost stays at one DB call.

    * ``total_amount``     — SUM of ``Adjudicacion.amount_uyu`` over the
      filtered set; rows with NULL ``amount_uyu`` are excluded from the
      sum. Coalesced to ``Decimal("0")`` so the UI can render a clean
      "0" instead of NULL.
    * ``average_amount``   — ``total_amount / non_null_count`` of the
      same filtered set. When no non-null amount exists, falls back to
      ``Decimal("0")`` (the empty-state value per the spec).
    * ``purchase_count``   — COUNT(DISTINCT ``Compra.id``) over the
      filtered set (a Compra with N adjudicaciones counts once).
    * ``company_count``    — COUNT(DISTINCT
      ``Adjudicacion.nombre_comercial``). ``nombre_comercial`` is
      declared NOT NULL in the schema, so the DISTINCT naturally
      excludes NULLs; this attribute is the number of distinct winning
      companies in the filtered set.
    """

    total_amount: Decimal
    average_amount: Decimal
    purchase_count: int
    company_count: int
    total: int


@dataclass(frozen=True)
class ConcentrationResult:
    """Result of the market-concentration aggregate.

    A compra (purchase) is "single bidder" if it has exactly one
    oferente; "multi bidder" if it has two or more. Purchases with
    zero oferentes are excluded from BOTH numerator and denominator
    (the metric is undefined for them — see the market-concentration
    spec, "Definition of the metric" scenario).

    * ``ratio``              — ``single_bidder_count / (single + multi)``,
      or ``None`` when the denominator is zero (no compras with
      oferentes match the filter). The empty-state branch in the UI
      uses the None signal to swap in the "Sin datos disponibles"
      message and skip the donut chart.
    * ``single_bidder_count`` — number of compras with exactly 1 oferente.
    * ``multi_bidder_count``  — number of compras with >= 2 oferentes.
    """

    ratio: Decimal | None
    single_bidder_count: int
    multi_bidder_count: int


@dataclass(frozen=True)
class RankingEntry:
    """One row in the top-N ranking by total adjudicated amount.

    Used by both the company ranking (top winning companies) and the
    organism ranking (top buyers). The HTML list partials render
    ``name`` as the row label, ``total_amount_uyu`` as the right-hand
    amount, and ``adjudication_count`` as a "N adjudicaciones"
    subline.

    * ``name``              — the display label (company or organism name).
    * ``total_amount_uyu``  — sum of ``Adjudicacion.amount_uyu`` for the
      group, in UYU. Rows with NULL ``amount_uyu`` are excluded from
      the aggregate; the sum is coalesced so the value is never NULL
      (empty groups don't make it past ``WHERE amount_uyu IS NOT NULL``).
    * ``adjudication_count`` — number of adjudicated line items (NOT
      distinct compras) the group contributed to the total. A company
      that won three different line items on the same compra shows as
      ``adjudication_count == 3`` — what the spec describes as
      adjudicaciones, not distinct purchases.
    """

    name: str
    total_amount_uyu: Decimal
    adjudication_count: int
    company_type: str | None = None
    company_number: str | None = None

    @property
    def company_profile_url(self) -> str | None:
        """Return the encoded company profile URL when identity is complete."""

        if not self.company_type or not self.company_number:
            return None
        return (
            f"/company/{quote(self.company_type, safe='')}/"
            f"{quote(self.company_number, safe='')}"
        )


@dataclass(frozen=True)
class ArticleRanking:
    """One row in the top articles ranking for a filtered profile."""

    name: str
    article_id: str | None
    total_amount_uyu: Decimal
    adjudication_count: int


def ranking_by_company(
    session: Session,
    filters: AdjudicationFilters,
    *,
    limit: int = 10,
) -> list[RankingEntry]:
    """Return the top companies by total adjudicated amount in UYU.

    Each :class:`RankingEntry` carries the company name, the SUM of
    ``Adjudicacion.amount_uyu`` for that company, and the count of
    adjudicated line items the company contributed. Rows with NULL
    ``amount_uyu`` (non-convertible currencies) are excluded from
    both the SUM and the count so they do not skew the totals.

    The ranking MUST reflect the same filters as the listing (see
    ranking-visualization spec, "Ranking reflects active filters"
    scenario).
    """

    pair_count = func.count(Adjudicacion.id)
    latest_date = func.max(Compra.fecha_pub_adj)
    identity_stmt = (
        select(
            Adjudicacion.nombre_comercial.label("name"),
            Adjudicacion.tipo_doc_prov.label("company_type"),
            Adjudicacion.nro_doc_prov.label("company_number"),
            pair_count.label("pair_count"),
            latest_date.label("latest_date"),
        )
        .join(Compra, Compra.id == Adjudicacion.compra_id)
        .where(
            Adjudicacion.amount_uyu.is_not(None),
            Adjudicacion.tipo_doc_prov.is_not(None),
            Adjudicacion.nro_doc_prov.is_not(None),
            Adjudicacion.tipo_doc_prov != "",
            Adjudicacion.nro_doc_prov != "",
        )
        .group_by(
            Adjudicacion.nombre_comercial,
            Adjudicacion.tipo_doc_prov,
            Adjudicacion.nro_doc_prov,
        )
    )
    identity_stmt = _apply_filters(identity_stmt, filters)
    identity_rows = identity_stmt.subquery("company_identity_counts")
    identity_ranked = select(
        identity_rows,
        func.row_number()
        .over(
            partition_by=identity_rows.c.name,
            order_by=(
                identity_rows.c.pair_count.desc(),
                identity_rows.c.latest_date.desc(),
                identity_rows.c.company_type.asc(),
                identity_rows.c.company_number.asc(),
            ),
        )
        .label("identity_rank"),
    ).subquery("company_identity_ranked")

    stmt = (
        select(
            Adjudicacion.nombre_comercial.label("name"),
            func.coalesce(func.sum(Adjudicacion.amount_uyu), 0).label(
                "total_amount_uyu"
            ),
            func.count(Adjudicacion.id).label("adjudication_count"),
            identity_ranked.c.company_type,
            identity_ranked.c.company_number,
        )
        .join(Compra, Compra.id == Adjudicacion.compra_id)
        .outerjoin(
            identity_ranked,
            and_(
                identity_ranked.c.name == Adjudicacion.nombre_comercial,
                identity_ranked.c.identity_rank == 1,
            ),
        )
        .where(Adjudicacion.amount_uyu.is_not(None))
        .group_by(Adjudicacion.nombre_comercial)
        .group_by(identity_ranked.c.company_type, identity_ranked.c.company_number)
        .order_by(func.sum(Adjudicacion.amount_uyu).desc())
        .limit(limit)
    )
    stmt = _apply_filters(stmt, filters)
    return [
        RankingEntry(
            name=row.name,
            total_amount_uyu=row.total_amount_uyu,
            adjudication_count=int(row.adjudication_count),
            company_type=row.company_type,
            company_number=row.company_number,
        )
        for row in session.execute(stmt)
    ]


def ranking_by_organism(
    session: Session,
    filters: AdjudicationFilters,
    *,
    limit: int = 10,
) -> list[RankingEntry]:
    """Return the top organisms by total adjudicated amount in UYU.

    Each :class:`RankingEntry` carries the organism name, the SUM of
    ``Adjudicacion.amount_uyu`` across that organism's adjudicaciones,
    and the count of adjudicated line items. Rows with NULL
    ``amount_uyu`` (non-convertible currencies) are excluded from
    both the SUM and the count so they do not skew the totals.

    The ranking MUST reflect the same filters as the listing (see
    organism-ranking-visualization spec, "Ranking reflects active
    filters" scenario).
    """

    stmt = (
        select(
            Compra.organismo.label("name"),
            func.coalesce(func.sum(Adjudicacion.amount_uyu), 0).label(
                "total_amount_uyu"
            ),
            func.count(Adjudicacion.id).label("adjudication_count"),
        )
        .join(Compra, Compra.id == Adjudicacion.compra_id)
        .where(Adjudicacion.amount_uyu.is_not(None))
        .group_by(Compra.organismo)
        .order_by(func.sum(Adjudicacion.amount_uyu).desc())
        .limit(limit)
    )
    stmt = _apply_filters(stmt, filters)
    return [
        RankingEntry(
            name=row.name,
            total_amount_uyu=row.total_amount_uyu,
            adjudication_count=int(row.adjudication_count),
        )
        for row in session.execute(stmt)
    ]


MAX_TOP_ARTICLES: int = 20


def top_articles(
    session: Session,
    filters: AdjudicationFilters,
    *,
    limit: int = MAX_TOP_ARTICLES,
) -> list[ArticleRanking]:
    """Return top articles grouped by ID when available, otherwise description."""

    article_key = func.coalesce(Adjudicacion.id_articulo, Adjudicacion.desc_articulo)
    effective_limit = min(max(limit, 0), MAX_TOP_ARTICLES)
    stmt = (
        select(
            func.min(Adjudicacion.desc_articulo).label("name"),
            func.min(Adjudicacion.id_articulo).label("article_id"),
            func.coalesce(func.sum(Adjudicacion.amount_uyu), 0).label(
                "total_amount_uyu"
            ),
            func.count(Adjudicacion.id).label("adjudication_count"),
        )
        .join(Compra, Compra.id == Adjudicacion.compra_id)
        .where(Adjudicacion.amount_uyu.is_not(None))
        .group_by(article_key)
        .order_by(func.sum(Adjudicacion.amount_uyu).desc())
        .limit(effective_limit)
    )
    stmt = _apply_filters(stmt, filters)
    return [
        ArticleRanking(
            name=row.name,
            article_id=row.article_id,
            total_amount_uyu=Decimal(row.total_amount_uyu or 0),
            adjudication_count=int(row.adjudication_count),
        )
        for row in session.execute(stmt)
    ]


def kpi_summary(session: Session, filters: AdjudicationFilters) -> KpiSummary:
    """Return a single snapshot of summary metrics for the filtered set.

    The four aggregates are computed in one round-trip; the row count
    of non-null ``amount_uyu`` values is fetched alongside the sum so
    the average can be derived without a second query (the spec says
    "average uses the same filtered set" — same predicates, no extra
    scope).

    Filters on the Adjudicacion side (company / article / article_id)
    and the Compra side (organism / date_from / date_to) are honoured
    via :func:`_apply_filters`. Rows with NULL ``amount_uyu`` are
    excluded from both the SUM and the COUNT used for the average, so
    non-convertible currencies do not skew the total or divide the
    average.

    Returns a :class:`KpiSummary` whose values are safe to render
    directly: ``total_amount`` is coalesced to ``Decimal("0")`` and
    ``average_amount`` falls back to ``Decimal("0")`` when the
    filtered set has zero non-null amounts. ``purchase_count`` and
    ``company_count`` come from COUNT(DISTINCT …) so they cannot be
    NULL.
    """

    non_null_amount_count = func.coalesce(
        func.sum(case((Adjudicacion.amount_uyu.is_not(None), 1), else_=0)),
        0,
    ).label("non_null_count")
    total_amount_expr = func.coalesce(func.sum(Adjudicacion.amount_uyu), 0).label(
        "total_amount"
    )
    purchase_count_expr = func.count(func.distinct(Compra.id)).label("purchase_count")
    company_count_expr = func.count(func.distinct(Adjudicacion.nombre_comercial)).label(
        "company_count"
    )
    total_expr = func.count(Adjudicacion.id).label("total")

    stmt = select(
        total_amount_expr,
        non_null_amount_count,
        purchase_count_expr,
        company_count_expr,
        total_expr,
    ).join(Compra, Compra.id == Adjudicacion.compra_id)
    stmt = _apply_filters(stmt, filters)

    row = session.execute(stmt).one()
    total = Decimal(row.total_amount or 0)
    non_null = int(row.non_null_count or 0)
    average = total / Decimal(non_null) if non_null > 0 else Decimal(0)
    return KpiSummary(
        total_amount=total,
        average_amount=average,
        purchase_count=int(row.purchase_count or 0),
        company_count=int(row.company_count or 0),
        total=int(row.total or 0),
    )


def monthly_trend(
    session: Session, filters: AdjudicationFilters
) -> list[tuple[str, Decimal]]:
    """Return monthly totals of adjudicated amount in UYU for the filtered set.

    The result is a list of ``(YYYY-MM, total_uyu)`` pairs in
    chronological order. Sparse months — months within the active
    window that have no adjudicaciones — are filled in with a value of
    ``Decimal(0)`` so the X-axis of the trend chart has no gaps
    (temporal-trend spec, "Sparse months are preserved" scenario).

    The window is determined by, in order of preference:

    1. The active ``date_from`` / ``date_to`` filters, when both are
       present (the route always injects the current-year window on
       cold load, so the active window is almost always available).
    2. The data extent — from the earliest to the latest non-empty
       month in the result. Used when no date filter is set so the
       trend still covers every observed month with zero-fill.

    Rows with NULL ``amount_uyu`` are excluded from the aggregation
    (consistent with the company / organism rankings and with the
    empty-state spec — "Data exists but all amounts are null" shows
    the "Sin datos disponibles" message).

    Returns ``[]`` when the filtered set has no adjudicaciones with
    non-null ``amount_uyu``; the route translates an empty result
    into the empty-state message.
    """

    # Dialect-aware month formatting: strftime for SQLite (tests),
    # to_char for PostgreSQL (production).
    bind = session.bind
    if bind is None:
        raise RuntimeError("session is not bound to an engine")
    dialect = bind.dialect.name
    if dialect == "postgresql":
        ym_expr = func.to_char(Compra.fecha_pub_adj, "YYYY-MM").label("ym")
    else:
        ym_expr = func.strftime("%Y-%m", Compra.fecha_pub_adj).label("ym")
    total_expr = func.coalesce(func.sum(Adjudicacion.amount_uyu), 0).label("total")

    stmt = select(ym_expr, total_expr).join(Compra, Compra.id == Adjudicacion.compra_id)
    stmt = stmt.where(Adjudicacion.amount_uyu.is_not(None))
    stmt = stmt.group_by(ym_expr)
    stmt = _apply_filters(stmt, filters)

    rows = [(row.ym, Decimal(row.total)) for row in session.execute(stmt)]
    if not rows:
        return []

    # Determine the fill range. Use the data's natural extent so
    # the line only reaches the last month with actual data — no
    # trailing zero months that produce NaN tooltips on future dates.
    first_label, _ = min(rows, key=lambda r: r[0])
    last_label, _ = max(rows, key=lambda r: r[0])
    start = date(int(first_label[:4]), int(first_label[5:7]), 1)
    end = date(int(last_label[:4]), int(last_label[5:7]), 1)

    data_by_label = dict(rows)
    result: list[tuple[str, Decimal]] = []
    current = start
    # ``end`` is included; we walk month-by-month until we pass it.
    while current <= end:
        label = f"{current.year:04d}-{current.month:02d}"
        result.append((label, Decimal(data_by_label.get(label, 0))))
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return result


def concentration_ratio(
    session: Session, filters: AdjudicationFilters
) -> ConcentrationResult:
    """Return the single-bidder share of the filtered set.

    Counts compras (purchases) by their oferente count: a compra with
    exactly one oferente is "single bidder", two or more is
    "multi bidder"; compras with zero oferentes are excluded from
    BOTH the numerator and the denominator (the metric is undefined
    for them, see the market-concentration spec).

    Oferentes are grouped once by ``compra_id`` in a derived table, then
    inner-joined to the distinct compras whose adjudicaciones match the
    active filters. This keeps zero-oferente compras and compras without a
    matching adjudicacion out of both buckets while remaining portable to
    SQLite and PostgreSQL.

    ``ratio`` is ``None`` when no compras in the filtered set have
    any oferentes at all (denominator is zero). The route / template
    treats that as the empty state and skips the donut chart.
    """

    of_counts = (
        select(
            Oferente.compra_id.label("compra_id"),
            func.count(Oferente.id).label("of_count"),
        )
        .group_by(Oferente.compra_id)
        .subquery("of_counts")
    )

    # A Compra is in scope when at least one of its Adjudicaciones matches
    # the filter (organism / company / article / article_id / date range).
    matching = (
        select(Compra.id.label("id"))
        .join(Adjudicacion, Adjudicacion.compra_id == Compra.id)
        .distinct()
    )
    matching = _apply_filters(matching, filters).subquery("matching")

    single_expr = func.coalesce(
        func.sum(case((of_counts.c.of_count == 1, 1), else_=0)), 0
    ).label("single")
    multi_expr = func.coalesce(
        func.sum(case((of_counts.c.of_count >= 2, 1), else_=0)), 0
    ).label("multi")

    stmt = select(single_expr, multi_expr).select_from(of_counts).join(
        matching, matching.c.id == of_counts.c.compra_id
    )

    row = session.execute(stmt).one()
    single = int(row.single)
    multi = int(row.multi)
    total = single + multi
    ratio = Decimal(single) / Decimal(total) if total > 0 else None
    return ConcentrationResult(
        ratio=ratio,
        single_bidder_count=single,
        multi_bidder_count=multi,
    )


def _is_organism_predicate(predicate: Any) -> bool:
    """Return ``True`` if ``predicate`` is any organism filter (ILIKE or exact).

    Used by :func:`distinct_organisms` to drop the organism filter from
    the predicate list — that way the dropdown can suggest names from
    the remaining dimensions even when an organism is already selected.
    The exact-match predicate (used by the organism profile route) is
    recognised alongside the existing ILIKE predicate so the
    suggestions behave consistently regardless of which filter form
    is active.
    """

    if not hasattr(predicate, "left") or not hasattr(predicate, "operator"):
        return False
    left = predicate.left
    operator_name = getattr(predicate.operator, "__name__", "")
    return (
        isinstance(left, Column)
        and left.key == "organismo"
        and operator_name in {"ilike_op", "eq_op"}
    )


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
        select(Compra.organismo)
        .join(Adjudicacion, Adjudicacion.compra_id == Compra.id)
        .distinct()
        .order_by(Compra.organismo.asc())
        .limit(limit)
    )
    if predicates:
        stmt = stmt.where(and_(*predicates))
    return [row[0] for row in session.execute(stmt) if row[0] is not None]
