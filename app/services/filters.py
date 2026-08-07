"""Filter value objects, validation, and SQLAlchemy predicate construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import and_

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra


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
    default-injection job (see ``app.routes.common``). Validating
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
        predicates.append(Compra.organismo == filters.organism_exact)
    if filters.article:
        predicates.append(
            Adjudicacion.desc_articulo.ilike(
                f"%{_escape_like(filters.article)}%", escape="\\"
            )
        )
    if filters.article_id:
        # Comma-separated list of exact IDs → IN set predicate. Whitespace
        # and empty entries are dropped so trailing commas ("1234, ")
        # do not pollute the lookup. NULLs are excluded from IN by SQL
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
