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
from typing import TYPE_CHECKING, Any

from sqlalchemy import Column, and_, func, select

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra

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
    amount: "Decimal"
    currency: str
    amount_uyu: "Decimal | None"
    license_type: str
    company_document: str | None
    company_document_type: str | None
    license_link: str


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
        and left.key == "organismo"
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
        predicates.append(Adjudicacion.nombre_comercial.ilike(f"%{filters.company}%"))
    if filters.organism:
        predicates.append(Compra.organismo.ilike(f"%{filters.organism}%"))
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

    stmt = select(func.count(Adjudicacion.id)).join(Compra, Compra.id == Adjudicacion.compra_id)
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
) -> list[tuple[str, "Decimal"]]:
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
) -> list[tuple[str, "Decimal"]]:
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
        Adjudicacion.precio_tot_imp.label("amount"),
        Adjudicacion.amount_uyu.label("amount_uyu"),
        Adjudicacion.nro_doc_prov.label("company_document"),
        Adjudicacion.tipo_doc_prov.label("company_document_type"),
    ).join(Adjudicacion, Adjudicacion.compra_id == Compra.id)


def _row_to_adjudication_row(row: Any) -> AdjudicationRow:
    """Map a SQLAlchemy row to a display-shaped :class:`AdjudicationRow`.

    The ``currency`` field is the new schema's per-row display code
    (set by the normalizer at ingest). It is not on the new schema
    directly — the AdjudicacionRow constructor receives the resolved
    code from the scraper. For legacy rows that did not have a
    currency recorded, fall back to "UYU" so the template never
    renders ``None``.
    """

    currency = getattr(row, "currency", "UYU") or "UYU"
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
    )


__all__ = [
    "AdjudicationFilters",
    "AdjudicationRow",
    "DateValidationError",
    "count_adjudications",
    "distinct_organisms",
    "filters_from_query_params",
    "list_adjudications",
    "ranking_by_company",
    "ranking_by_organism",
    "validate_date_params",
]
