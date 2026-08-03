"""Application services for querying adjudications from the database.

This module is the only place the web layer talks to SQLAlchemy. Routes
call functions like :func:`list_adjudications` or :func:`ranking_by_company`
with plain Python values; the service builds the SQLAlchemy query, applies
active filters with AND logic, executes it, and returns plain data
structures (lists of :class:`AdjudicationRow` dataclasses for listings,
``(label, value)`` pairs for charts, lists of :class:`RankingEntry`
dataclasses for the top-N rankings, etc.).

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
    build_license_link,
    display_currency,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

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
    company_doc_exact: tuple[str, str] | None = None
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
                "company_doc_exact",
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
class CompanyProfileSummary:
    """Company-profile KPIs for one exact provider-document identity."""

    display_name: str | None
    total_amount: Decimal
    purchase_count: int
    organism_count: int
    share_of_total: Decimal


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


@dataclass(frozen=True)
class ArticleRanking:
    """One row in the top articles ranking for a filtered profile."""

    name: str
    article_id: str | None
    total_amount_uyu: Decimal
    adjudication_count: int


def _normalize(text: str | None) -> str | None:
    """Strip and collapse a user-typed string, returning ``None`` if empty."""

    if text is None:
        return None
    stripped = text.strip()
    return stripped or None


# Bound the ``article_id`` filter so a malicious or accidental huge list
# cannot produce an oversized SQL ``IN`` clause.
_MAX_ARTICLE_IDS = 200
_MAX_ARTICLE_ID_RAW_LENGTH = 4096


def _escape_like(value: str) -> str:
    """Escape SQL LIKE wildcards so user input matches literally."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ValidationError(ValueError):
    """Raised when a user-supplied filter value is invalid.

    The route layer catches this and returns HTTP 422 with an HTML
    fragment suitable for HTMX swap. We use ``ValueError`` as the base
    so callers that broadly handle ``ValueError`` continue to work.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class DateValidationError(ValidationError):
    """Raised when ``date_from`` or ``date_to`` are present but invalid."""


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
        # 3. Reject range wider than 5 years (1825 days = 5*365, leap-year safe).
        if (dto - dfrom).days > 1825:
            raise DateValidationError("El rango de fechas no puede superar los 5 años.")


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
        predicates.append(
            Adjudicacion.nombre_comercial.ilike(
                f"%{_escape_like(filters.company)}%", escape="\\"
            )
        )
    if filters.company_doc_exact is not None:
        company_type, company_number = filters.company_doc_exact
        predicates.extend(
            [
                Adjudicacion.tipo_doc_prov == company_type,
                Adjudicacion.nro_doc_prov == company_number,
            ]
        )
    if filters.organism:
        predicates.append(
            Compra.organismo.ilike(f"%{_escape_like(filters.organism)}%", escape="\\")
        )
    if filters.organism_exact:
        # Exact-match predicate (organism profile). Equality on
        # ``organismo`` avoids the ambiguity of ILIKE partial match
        # when the user has clicked through from a known name — see
        # the organism-profile spec, "Profile inherits the dashboard
        # widgets" requirement.
        predicates.append(Compra.organismo == filters.organism_exact)
    if filters.article:
        predicates.append(
            Adjudicacion.desc_articulo.ilike(
                f"%{_escape_like(filters.article)}%", escape="\\"
            )
        )
    if filters.article_id:
        # Comma-separated list of exact IDs → IN set predicate. Whitespace
        # and empty entries are dropped so trailing commas ("1234, ") do
        # not pollute the lookup. NULLs are excluded from IN by SQL
        # semantics, matching the spec.
        if len(filters.article_id) > _MAX_ARTICLE_ID_RAW_LENGTH:
            raise ValidationError("El filtro de IDs de artículo es demasiado largo.")
        ids = [piece.strip() for piece in filters.article_id.split(",")]
        ids = [piece for piece in ids if piece]
        if len(ids) > _MAX_ARTICLE_IDS:
            raise ValidationError(
                f"El filtro de IDs no puede tener más de {_MAX_ARTICLE_IDS} valores."
            )
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
# Streaming export
# ---------------------------------------------------------------------------

MAX_EXPORT_ROWS: int = 500_000
MAX_TOP_ARTICLES: int = 20


def iter_adjudications(
    session: Session,
    filters: AdjudicationFilters,
    *,
    chunk_size: int = 1000,
) -> Iterator[AdjudicationRow]:
    """Yield filtered adjudication rows newest-first, using ``_listing_query``.

    The generator uses ``yield_per(chunk_size)`` so the DB driver fetches
    rows in batches instead of loading the entire result set into memory.
    The caller (the route layer) is responsible for closing the session
    when iteration is complete.
    """

    stmt = _listing_query()
    stmt = _apply_filters(stmt, filters)
    stmt = stmt.order_by(Compra.fecha_pub_adj.desc(), Adjudicacion.id.desc())
    stmt = stmt.execution_options(yield_per=chunk_size)
    for row in session.execute(stmt):
        yield _row_to_adjudication_row(row)


# ---------------------------------------------------------------------------
# Chart aggregations
# ---------------------------------------------------------------------------


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

    stmt = (
        select(
            Adjudicacion.nombre_comercial.label("name"),
            func.coalesce(func.sum(Adjudicacion.amount_uyu), 0).label(
                "total_amount_uyu"
            ),
            func.count(Adjudicacion.id).label("adjudication_count"),
        )
        .join(Compra, Compra.id == Adjudicacion.compra_id)
        .where(Adjudicacion.amount_uyu.is_not(None))
        .group_by(Adjudicacion.nombre_comercial)
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


def lookup_company_identity(
    session: Session, company_type: str, company_number: str
) -> str | None:
    """Return the latest commercial name for an exact document pair."""

    stmt = (
        select(Adjudicacion.nombre_comercial)
        .join(Compra, Compra.id == Adjudicacion.compra_id)
        .where(
            Adjudicacion.tipo_doc_prov == company_type,
            Adjudicacion.nro_doc_prov == company_number,
        )
        .order_by(Compra.fecha_pub_adj.desc(), Adjudicacion.id.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def company_summary(
    session: Session, filters: AdjudicationFilters
) -> CompanyProfileSummary:
    """Return exact-document KPIs and share of the filtered market total."""

    total_expr = func.coalesce(func.sum(Adjudicacion.amount_uyu), 0).label(
        "total_amount"
    )
    purchase_expr = func.count(func.distinct(Compra.id)).label("purchase_count")
    organism_expr = func.count(func.distinct(Compra.organismo)).label("organism_count")
    company_stmt = select(total_expr, purchase_expr, organism_expr).join(
        Compra, Compra.id == Adjudicacion.compra_id
    )
    company_stmt = _apply_filters(company_stmt, filters)
    company_row = session.execute(company_stmt).one()
    total = Decimal(company_row.total_amount or 0)

    market_filters = AdjudicationFilters(
        company=filters.company,
        organism=filters.organism,
        organism_exact=filters.organism_exact,
        article=filters.article,
        article_id=filters.article_id,
        date_from=filters.date_from,
        date_to=filters.date_to,
    )
    market_stmt = select(func.coalesce(func.sum(Adjudicacion.amount_uyu), 0)).join(
        Compra, Compra.id == Adjudicacion.compra_id
    )
    market_stmt = _apply_filters(market_stmt, market_filters)
    market_total = Decimal(session.execute(market_stmt).scalar_one() or 0)
    share = total / market_total if market_total > 0 else Decimal(0)

    return CompanyProfileSummary(
        display_name=None,
        total_amount=total,
        purchase_count=int(company_row.purchase_count or 0),
        organism_count=int(company_row.organism_count or 0),
        share_of_total=share,
    )


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


def all_organisms(session: Session) -> list[str]:
    """Return every distinct organism name in the database, no limit.

    Used by the sitemap.xml route to enumerate all publicly crawlable
    organism pages. Unlike :func:`distinct_organisms` (which filters
    and caps at ``limit`` for the datalist), this query returns the
    complete unfiltered set so the sitemap stays current as new
    organisms appear.
    """

    stmt = (
        select(Compra.organismo)
        .join(Adjudicacion, Adjudicacion.compra_id == Compra.id)
        .distinct()
        .order_by(Compra.organismo.asc())
    )
    return [row[0] for row in session.execute(stmt) if row[0] is not None]


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


def _row_to_adjudication_row(row: Any) -> AdjudicationRow:
    """Map a SQLAlchemy row to a display-shaped :class:`AdjudicationRow`.

    The ``currency`` field is derived at query time from
    ``id_moneda`` (see :func:`scraper.normalizer.display_currency`); the database does
    not store the display code, and the per-line-item conversion
    tables live in :mod:`scraper.normalizer`. Unknown codes fall back
    to ``"N/D"`` so the template never renders a blank currency.
    """

    id_moneda = getattr(row, "id_moneda", None)
    currency = display_currency(id_moneda) if id_moneda is not None else "N/D"
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
        license_link=build_license_link(row.id_compra),
        article_id=getattr(row, "article_id", None),
    )


__all__ = [
    "AdjudicationFilters",
    "AdjudicationRow",
    "ArticleRanking",
    "CompanyProfileSummary",
    "ConcentrationResult",
    "DateValidationError",
    "KpiSummary",
    "MAX_EXPORT_ROWS",
    "MAX_TOP_ARTICLES",
    "RankingEntry",
    "ValidationError",
    "all_organisms",
    "concentration_ratio",
    "company_summary",
    "count_adjudications",
    "distinct_organisms",
    "filters_from_query_params",
    "iter_adjudications",
    "kpi_summary",
    "list_adjudications",
    "lookup_company_identity",
    "monthly_trend",
    "ranking_by_company",
    "ranking_by_organism",
    "top_articles",
    "validate_date_params",
]
