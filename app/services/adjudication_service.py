"""Application services for querying adjudications from the database.

This module is the only place the web layer talks to SQLAlchemy. Routes
call functions like :func:`list_adjudications` or :func:`ranking_by_company`
with plain Python values; the service builds the SQLAlchemy query, applies
active filters with AND logic, executes it, and returns plain data
structures (lists of :class:`AdjudicationRow` dataclasses for listings,
``(label, value)`` pairs for charts, etc.).

The query layer reads from a join of the :class:`Compra` and
:class:`Adjudicacion` tables (web-app spec, "Query Layer Reads From
New Schema" requirement). The service flattens each row into an
:class:`AdjudicationRow` — a display-shaped dataclass whose attribute
names match the old ORM model so the existing Jinja templates do not
need to change. The mapping is one-to-one:

* ``date``     ← ``Compra.fecha_pub_adj``
* ``organism`` ← ``Compra.organismo``
* ``license_type`` ← ``Compra.id_tipocompra``
* ``license_link``  ← built from ``Compra.id_compra``
* ``winning_company``  ← ``Adjudicacion.nombre_comercial``
* ``article``  ← ``Adjudicacion.desc_articulo``
* ``amount``   ← ``Adjudicacion.precio_tot_imp``
* ``currency`` ← resolved at ingest (no longer a column — see
   :mod:`scraper.normalizer` for the display code table)
* ``amount_uyu`` ← ``Adjudicacion.amount_uyu``
* ``company_document``        ← ``Adjudicacion.nro_doc_prov``
* ``company_document_type``   ← ``Adjudicacion.tipo_doc_prov``
* ``article_id``              ← ``Adjudicacion.id_articulo``

Keeping the query construction in one place means:

* Routes stay thin and free of SQLAlchemy specifics.
* Filter parsing and validation live next to the query they affect.
* Tests can drive the service directly with an in-memory SQLite session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import Column, and_, case, func, select

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from app.models.oferente import Oferente
from scraper.normalizer import (
    CONVERSION_TABLE,
    NON_CONVERTIBLE_TABLE,
    PASSTHROUGH_TABLE,
)

if TYPE_CHECKING:
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
    organism_exact: str | None = None
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
                "organism_exact",
                "article",
                "article_id",
                "date_from",
                "date_to",
            )
        )


# ---------------------------------------------------------------------------
# Display dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdjudicationRow:
    """Display-shaped view of one adjudicated line item.

    Returned by the service so the Jinja templates can read the same
    field names they used to read off the old ``Adjudication`` ORM
    model. Construction happens inside the service — the web layer
    never builds one by hand.
    """

    date: date
    organism: str
    winning_company: str
    article: str
    amount: Decimal
    currency: str
    amount_uyu: Decimal | None
    license_type: str
    company_document: str | None
    company_document_type: str | None
    license_link: str
    article_id: str | None = None


# ---------------------------------------------------------------------------
# Aggregate result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KpiSummary:
    """Summary metrics for the currently filtered adjudication set.

    The four values are produced by a single query so the snapshot is
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


# ---------------------------------------------------------------------------
# License link template — mirrors the deterministic template the
# scraper uses when building its own link, so DB rows and freshly
# scraped rows produce the same URL for the same ``id_compra``.
# ---------------------------------------------------------------------------

_LICENSE_LINK_TEMPLATE = (
    "https://www.comprasestatales.gub.uy/consultas/detalle/id/{id_compra}"
)


def _build_license_link(id_compra: str) -> str:
    return _LICENSE_LINK_TEMPLATE.format(id_compra=id_compra)


def _normalize(text: str | None) -> str | None:
    """Strip and collapse a user-typed string, returning ``None`` if empty."""

    if text is None:
        return None
    stripped = text.strip()
    return stripped or None


class DateValidationError(ValueError):
    """Raised when ``date_from`` or ``date_to`` are present but invalid.

    The route layer catches this and returns HTTP 422 with an HTML
    fragment suitable for HTMX swap. We use ``ValueError`` as the base
    so callers that broadly handle ``ValueError`` continue to work.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def validate_date_params(params: dict[str, str | None]) -> None:
    """Validate raw ``date_from``/``date_to`` query parameters.

    Raises :class:`DateValidationError` when either value is present but
    not a valid ISO 8601 ``YYYY-MM-DD`` date, or when both are present
    and ``date_from > date_to``.

    Silent on missing/empty params — that is the route layer's
    default-injection job (see ``app.routes.adjudications``). Validating
    raw strings (instead of the parsed ``AdjudicationFilters``) lets us
    distinguish "user typed garbage" from "user typed nothing", which the
    parsed form collapses into ``None``.
    """

    # 1. Reject unparseable date strings.
    for key in ("date_from", "date_to"):
        raw = params.get(key)
        if raw is None:
            continue
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            date.fromisoformat(stripped)
        except ValueError as exc:
            raise DateValidationError(
                "Formato de fecha inválido. Use AAAA-MM-DD."
            ) from exc

    # 2. Reject reversed range.
    dfrom_raw = params.get("date_from")
    dto_raw = params.get("date_to")
    if dfrom_raw and dfrom_raw.strip() and dto_raw and dto_raw.strip():
        dfrom = date.fromisoformat(dfrom_raw.strip())
        dto = date.fromisoformat(dto_raw.strip())
        if dfrom > dto:
            raise DateValidationError(
                "La fecha 'Desde' no puede ser posterior a la fecha 'Hasta'."
            )


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
        predicates.append(Adjudicacion.nombre_comercial.ilike(f"%{filters.company}%"))
    if filters.organism:
        predicates.append(Compra.organismo.ilike(f"%{filters.organism}%"))
    if filters.organism_exact:
        # Exact-match predicate (organism profile). Equality on
        # ``organismo`` avoids the ambiguity of ILIKE partial match
        # when the user has clicked through from a known name — see
        # the organism-profile spec, "Profile inherits the dashboard
        # widgets" requirement.
        predicates.append(Compra.organismo == filters.organism_exact)
    if filters.article:
        predicates.append(Adjudicacion.desc_articulo.ilike(f"%{filters.article}%"))
    if filters.article_id:
        # Comma-separated list of exact IDs → IN set predicate. Whitespace
        # and empty entries are dropped so trailing commas ("1234, ") do
        # not pollute the lookup. NULLs are excluded from IN by SQL
        # semantics, matching the spec.
        ids = [piece.strip() for piece in filters.article_id.split(",")]
        ids = [piece for piece in ids if piece]
        if ids:
            predicates.append(Adjudicacion.id_articulo.in_(ids))
    if filters.date_from is not None:
        predicates.append(Compra.fecha_pub_adj >= filters.date_from)
    if filters.date_to is not None:
        predicates.append(Compra.fecha_pub_adj <= filters.date_to)

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
) -> list[AdjudicationRow]:
    """Return a page of adjudicated line items matching ``filters``, newest first.

    Joins :class:`Compra` and :class:`Adjudicacion`; orders by
    ``Compra.fecha_pub_adj DESC, Adjudicacion.id DESC`` so two line
    items on the same date have a stable order. ``limit`` and ``offset``
    are simple pagination knobs — the route layer may cap them.
    """

    stmt = _listing_query()
    stmt = _apply_filters(stmt, filters)
    stmt = stmt.order_by(Compra.fecha_pub_adj.desc(), Adjudicacion.id.desc())
    stmt = stmt.limit(limit).offset(offset)
    return [_row_to_adjudication_row(row) for row in session.execute(stmt)]


def count_adjudications(session: Session, filters: AdjudicationFilters) -> int:
    """Return the total number of adjudicaciones matching ``filters``.

    Used by the route to render pagination controls and the "showing N
    of M" header. A separate ``COUNT(*)`` query keeps the listing query
    simple.
    """

    stmt = select(func.count(Adjudicacion.id)).join(
        Compra, Compra.id == Adjudicacion.compra_id
    )
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
            Adjudicacion.nombre_comercial.label("company"),
            func.coalesce(func.sum(Adjudicacion.amount_uyu), 0).label("total"),
        )
        .join(Compra, Compra.id == Adjudicacion.compra_id)
        .where(Adjudicacion.amount_uyu.is_not(None))
        .group_by(Adjudicacion.nombre_comercial)
        .order_by(func.sum(Adjudicacion.amount_uyu).desc())
        .limit(limit)
    )
    stmt = _apply_filters(stmt, filters)
    return [(row.company, row.total) for row in session.execute(stmt)]


def ranking_by_organism(
    session: Session,
    filters: AdjudicationFilters,
    *,
    limit: int = 10,
) -> list[tuple[str, Decimal]]:
    """Return the top organisms by total adjudicated amount in UYU.

    The result is a list of ``(organism_name, total_amount_uyu)`` pairs,
    sorted descending by amount. ``amount_uyu`` may be ``NULL`` in the
    database (non-convertible currencies); those rows are excluded from
    the ranking so they do not skew the totals.

    The ranking MUST reflect the same filters as the listing (see
    organism-ranking-visualization spec, "Chart reflects active filters"
    scenario).
    """

    stmt = (
        select(
            Compra.organismo.label("organism"),
            func.coalesce(func.sum(Adjudicacion.amount_uyu), 0).label("total"),
        )
        .join(Compra, Compra.id == Adjudicacion.compra_id)
        .where(Adjudicacion.amount_uyu.is_not(None))
        .group_by(Compra.organismo)
        .order_by(func.sum(Adjudicacion.amount_uyu).desc())
        .limit(limit)
    )
    stmt = _apply_filters(stmt, filters)
    return [(row.organism, row.total) for row in session.execute(stmt)]


# ---------------------------------------------------------------------------
# Dashboard aggregates (KPI summary, monthly trend, market concentration)
# ---------------------------------------------------------------------------


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

    stmt = select(
        total_amount_expr,
        non_null_amount_count,
        purchase_count_expr,
        company_count_expr,
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
    dialect = session.bind.dialect.name
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

    The per-compra oferente count is computed via a correlated
    subquery (``scalar_subquery`` on ``Oferente.compra_id``). This
    keeps the whole aggregate to a single SQL trip, and the
    correlated-subquery form is portable to SQLite — the test
    database — as well as PostgreSQL.

    ``ratio`` is ``None`` when no compras in the filtered set have
    any oferentes at all (denominator is zero). The route / template
    treats that as the empty state and skips the donut chart.
    """

    oferente_count = (
        select(func.count(Oferente.id))
        .where(Oferente.compra_id == Compra.id)
        .correlate(Compra)
        .scalar_subquery()
    )

    # Comprehensively apply the same filters as the listing: a Compra
    # is in scope when at least one of its Adjudicaciones matches the
    # filter (organism / company / article / article_id / date range).
    # We resolve the matching Compra IDs in a subquery, then bucket
    # them by their oferente count.
    matching_compras = (
        select(Compra.id)
        .join(Adjudicacion, Adjudicacion.compra_id == Compra.id)
        .distinct()
    )
    matching_compras = _apply_filters(matching_compras, filters).subquery()

    single_expr = func.coalesce(
        func.sum(case((oferente_count == 1, 1), else_=0)), 0
    ).label("single")
    multi_expr = func.coalesce(
        func.sum(case((oferente_count >= 2, 1), else_=0)), 0
    ).label("multi")

    stmt = select(single_expr, multi_expr).where(
        Compra.id.in_(select(matching_compras))
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
        select(Compra.organismo)
        .join(Adjudicacion, Adjudicacion.compra_id == Compra.id)
        .distinct()
        .order_by(Compra.organismo.asc())
        .limit(limit)
    )
    if predicates:
        stmt = stmt.where(and_(*predicates))
    return [row[0] for row in session.execute(stmt) if row[0] is not None]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _listing_query() -> Any:
    """Base SELECT for the listing query — selects all display fields.

    Returning a column bundle keeps ``list_adjudications`` focused on
    ordering + limits + filters; this helper centralizes the projection
    so renaming a column only touches one place.
    """

    return select(
        Compra.fecha_pub_adj.label("date"),
        Compra.organismo.label("organism"),
        Compra.id_tipocompra.label("license_type"),
        Compra.id_compra.label("id_compra"),
        Adjudicacion.nombre_comercial.label("winning_company"),
        Adjudicacion.desc_articulo.label("article"),
        Adjudicacion.id_articulo.label("article_id"),
        Adjudicacion.precio_tot_imp.label("amount"),
        Adjudicacion.id_moneda.label("id_moneda"),
        Adjudicacion.amount_uyu.label("amount_uyu"),
        Adjudicacion.nro_doc_prov.label("company_document"),
        Adjudicacion.tipo_doc_prov.label("company_document_type"),
    ).join(Adjudicacion, Adjudicacion.compra_id == Compra.id)


def _display_currency(id_moneda: int | None) -> str:
    """Resolve ``id_moneda`` to the 3-letter display code shown in the table.

    The mapping mirrors :mod:`scraper.normalizer`: passthrough IDs render
    as ``"UYU"`` (no conversion), convertible IDs render as their ISO
    4217 code (e.g. ``"USD"``), and non-convertible IDs render as the
    custom placeholder (e.g. ``"UIX"``). Unknown or ``None`` IDs fall
    back to ``"N/D"`` so the template
    never sees a blank currency and the user knows the code is missing.
    """

    if id_moneda in PASSTHROUGH_TABLE:
        return PASSTHROUGH_TABLE[id_moneda]
    if id_moneda in CONVERSION_TABLE:
        return CONVERSION_TABLE[id_moneda][1]
    if id_moneda in NON_CONVERTIBLE_TABLE:
        return NON_CONVERTIBLE_TABLE[id_moneda]
    return "N/D"


def _row_to_adjudication_row(row: Any) -> AdjudicationRow:
    """Map a SQLAlchemy row to a display-shaped :class:`AdjudicationRow`.

    The ``currency`` field is derived at query time from
    ``id_moneda`` (see :func:`_display_currency`); the database does
    not store the display code, and the per-line-item conversion
    tables live in :mod:`scraper.normalizer`. Unknown codes fall back
    to ``"UYU"`` so the template never renders a blank currency.
    """

    currency = _display_currency(getattr(row, "id_moneda", None))
    organism = row.organism or ""
    license_type = row.license_type or ""
    return AdjudicationRow(
        date=row.date,
        organism=organism,
        winning_company=row.winning_company,
        article=row.article,
        amount=row.amount,
        currency=currency,
        amount_uyu=row.amount_uyu,
        license_type=license_type,
        company_document=row.company_document,
        company_document_type=row.company_document_type,
        license_link=_build_license_link(row.id_compra),
        article_id=getattr(row, "article_id", None),
    )


__all__ = [
    "AdjudicationFilters",
    "AdjudicationRow",
    "ConcentrationResult",
    "DateValidationError",
    "KpiSummary",
    "concentration_ratio",
    "count_adjudications",
    "distinct_organisms",
    "filters_from_query_params",
    "kpi_summary",
    "list_adjudications",
    "monthly_trend",
    "ranking_by_company",
    "ranking_by_organism",
    "validate_date_params",
]
