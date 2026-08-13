"""Company profile and company-scoped export routes."""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.database import get_db
from app.presenters import (
    _build_concentration_chart_payload,
    _build_page_numbers,
    _build_seo_context,
    _build_trend_chart_payload,
)
from app.routes._base import HeadAwareAPIRoute
from app.routes.common import (
    COMPETITOR_LIMIT,
    ORGANISM_SUGGEST_LIMIT,
    PAGE_SIZE,
    RANKING_LIMIT,
    _coerce_page,
    _enforce_identity_length,
    _full_page_validation_error,
    _inject_default_year_params,
    _render,
    _stream_csv_response,
    _validation_error_response,
)
from app.services.company import (
    CompanyCompetitor,
    CompanyProfileSummary,
    CompanyWinRate,
    _without_company_identity,
    company_competitors,
    company_summary,
    company_win_rate,
    lookup_company_identity,
)
from app.services.dashboard import (
    ConcentrationResult,
    concentration_ratio,
    distinct_organisms,
    kpi_summary,
    monthly_trend,
    ranking_by_organism,
    top_articles,
)
from app.services.filters import (
    AdjudicationFilters,
    ValidationError,
    filters_from_query_params,
    validate_date_params,
)
from app.services.listing import list_adjudications
from app.services.query_cache import cached_aggregate

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
router = APIRouter(route_class=HeadAwareAPIRoute)

# Match the database column limits on ``Adjudicacion`` — decoded company
# identities longer than these are rejected as missing resources before
# any identity-dependent query, filter, or CSV work runs.
MAX_COMPANY_DOCUMENT_TYPE_LENGTH = 10
MAX_COMPANY_DOCUMENT_NUMBER_LENGTH = 50


def _validated_company_identity(raw_type: str, raw_number: str) -> tuple[str, str]:
    """Decode and cap a company path identity before query construction.

    Returns the decoded (tipo_doc_prov, nro_doc_prov) tuple — the same
    values the routes already derive from unquote (no .strip(), no
    re-encoding) — or raises the standard 404 when either component
    exceeds its column limit.
    """

    decoded_type = _enforce_identity_length(
        unquote(raw_type), maximum=MAX_COMPANY_DOCUMENT_TYPE_LENGTH
    )
    decoded_number = _enforce_identity_length(
        unquote(raw_number), maximum=MAX_COMPANY_DOCUMENT_NUMBER_LENGTH
    )
    return decoded_type, decoded_number


def _company_filters(
    raw_type: str,
    raw_number: str,
    params: dict[str, str | None],
) -> AdjudicationFilters:
    """Build filters for a company document identity and shared query filters."""

    parsed = filters_from_query_params(params)
    return replace(
        parsed,
        company=None,
        company_doc_exact=(unquote(raw_type), unquote(raw_number)),
    )


def _build_company_context(
    db: Session,
    *,
    raw_type: str,
    raw_number: str,
    raw_params: dict[str, str | None],
) -> dict[str, Any]:
    """Build the shared full-page and HTMX company profile view-model."""

    decoded_type = unquote(raw_type)
    decoded_number = unquote(raw_number)
    _inject_default_year_params(raw_params)
    validate_date_params(raw_params)
    filters = _company_filters(raw_type, raw_number, raw_params)
    page = max(_coerce_page(raw_params.get("page")), 1)

    # One guard for the incomplete-identity path: no identity lookup,
    # listing, aggregate, or cache work happens for an empty decoded
    # document type or number.
    if not decoded_type or not decoded_number:
        return {
            "filters": filters,
            "company_type": decoded_type,
            "company_number": decoded_number,
            "company_name": None,
            "company_summary": CompanyProfileSummary(
                display_name=None,
                total_amount=Decimal("0"),
                total=0,
                purchase_count=0,
                organism_count=0,
                share_of_total=Decimal("0"),
            ),
            "results": [],
            "total": 0,
            "shown": 0,
            "page_size": PAGE_SIZE,
            "page": page,
            "total_pages": 1,
            "page_numbers": _build_page_numbers(page, 1),
            "ranking_rows": [],
            "top_article_rows": [],
            "organisms": [],
            "trend_rows": [],
            "trend_payload": _build_trend_chart_payload([]),
            "has_trend_data": False,
            "concentration": ConcentrationResult(None, 0, 0),
            "concentration_payload": None,
            "has_concentration_data": False,
            "company_win_rate": CompanyWinRate(0, 0, None),
            "company_competitors": [],
            "company_variant": True,
        }

    def win_rate_adapter(
        session: Session,
        selected_filters: AdjudicationFilters,
        *,
        limit: int | None = None,
    ) -> CompanyWinRate:
        del limit  # cache-key dimension only; never changes win-rate math
        return company_win_rate(session, decoded_type, decoded_number, selected_filters)

    def competitor_adapter(
        session: Session,
        selected_filters: AdjudicationFilters,
        *,
        limit: int | None = None,
    ) -> list[CompanyCompetitor]:
        selected_limit = COMPETITOR_LIMIT if limit is None else limit
        return company_competitors(
            session,
            decoded_type,
            decoded_number,
            selected_filters,
            limit=selected_limit,
        )

    identity_name = lookup_company_identity(db, decoded_type, decoded_number)
    market_filters = _without_company_identity(filters)
    market = cached_aggregate("kpi_summary", kpi_summary, db, market_filters)
    summary = replace(
        cached_aggregate(
            "company_summary",
            lambda s, f: company_summary(s, f, market_total=market.total_amount),
            db,
            filters,
        ),
        display_name=identity_name,
    )

    total = summary.total
    total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total > 0 else 1
    results = list_adjudications(
        db,
        filters,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
    )
    trend_rows = cached_aggregate("monthly_trend", monthly_trend, db, filters)
    concentration = cached_aggregate(
        "concentration_ratio", concentration_ratio, db, filters
    )
    win_rate = cached_aggregate(
        "company_win_rate",
        win_rate_adapter,
        db,
        filters,
        limit=RANKING_LIMIT,
    )
    competitors = cached_aggregate(
        "company_competitors",
        competitor_adapter,
        db,
        filters,
        limit=COMPETITOR_LIMIT,
    )
    ranking_rows = cached_aggregate(
        "ranking_by_organism",
        ranking_by_organism,
        db,
        filters,
        limit=RANKING_LIMIT,
    )
    top_article_rows = cached_aggregate(
        "top_articles",
        top_articles,
        db,
        filters,
        limit=RANKING_LIMIT,
    )
    organisms = cached_aggregate(
        "distinct_organisms",
        distinct_organisms,
        db,
        filters,
        limit=ORGANISM_SUGGEST_LIMIT,
    )
    page_numbers = _build_page_numbers(page, total_pages)

    return {
        "filters": filters,
        "company_type": decoded_type,
        "company_number": decoded_number,
        "company_name": identity_name,
        "company_summary": summary,
        "results": results,
        "total": total,
        "shown": len(results),
        "page_size": PAGE_SIZE,
        "page": page,
        "total_pages": total_pages,
        "page_numbers": page_numbers,
        "ranking_rows": ranking_rows,
        "top_article_rows": top_article_rows,
        "organisms": organisms,
        "trend_rows": trend_rows,
        "trend_payload": _build_trend_chart_payload(trend_rows),
        "has_trend_data": bool(trend_rows),
        "concentration": concentration,
        "concentration_payload": (
            _build_concentration_chart_payload(concentration, competition_labels=True)
            if concentration.ratio is not None
            else None
        ),
        "has_concentration_data": concentration.ratio is not None,
        "company_win_rate": win_rate,
        "company_competitors": competitors,
        "company_variant": True,
    }


@router.get(
    "/company/{tipo_doc_prov}/{nro_doc_prov}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def company_detail(
    tipo_doc_prov: str,
    nro_doc_prov: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    decoded_identity = _validated_company_identity(tipo_doc_prov, nro_doc_prov)
    raw_params = cast("dict[str, str | None]", dict(request.query_params))
    try:
        context = _build_company_context(
            db,
            raw_type=tipo_doc_prov,
            raw_number=nro_doc_prov,
            raw_params=raw_params,
        )
    except ValidationError as exc:
        return _full_page_validation_error(
            "company_detail.html",
            request,
            exc.message,
            raw_params=raw_params,
            company_identity=decoded_identity,
        )
    if context["page"] > context["total_pages"] and context["total"] > 0:
        return RedirectResponse(url=f"?page={context['total_pages']}", status_code=302)

    display_name = context["company_name"] or "Empresa sin actividad"
    context.update(
        _build_seo_context(
            meta_title=f"{display_name} — AdjudicaUY",
            meta_description=(
                f"Adjudicaciones de {display_name} en el Estado uruguayo."
            ),
            og_type="Corporation",
            path=(
                f"/company/{quote(context['company_type'], safe='')}/"
                f"{quote(context['company_number'], safe='')}"
            ),
        )
    )
    return _render("company_detail.html", request, context)


@router.get(
    "/company/{tipo_doc_prov}/{nro_doc_prov}/export",
    include_in_schema=False,
)
def export_company_adjudications(
    tipo_doc_prov: str,
    nro_doc_prov: str,
    request: Request,
) -> Response:
    """Stream the standard CSV export scoped to one company document."""

    _validated_company_identity(tipo_doc_prov, nro_doc_prov)
    params = cast("dict[str, str | None]", dict(request.query_params))
    _inject_default_year_params(params)
    try:
        validate_date_params(params)
    except ValidationError as exc:
        return Response(exc.message, status_code=422, media_type="text/plain")

    return _stream_csv_response(_company_filters(tipo_doc_prov, nro_doc_prov, params))


@router.get(
    "/company/{tipo_doc_prov}/{nro_doc_prov}/partial",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def company_detail_partial(
    tipo_doc_prov: str,
    nro_doc_prov: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    _validated_company_identity(tipo_doc_prov, nro_doc_prov)
    raw_params = cast("dict[str, str | None]", dict(request.query_params))
    try:
        context = _build_company_context(
            db,
            raw_type=tipo_doc_prov,
            raw_number=nro_doc_prov,
            raw_params=raw_params,
        )
    except ValidationError as exc:
        return _validation_error_response(exc.message)
    if context["page"] > context["total_pages"] and context["total"] > 0:
        return RedirectResponse(url=f"?page={context['total_pages']}", status_code=302)

    if raw_params.get("partial") == "table":
        return _render(
            "partials/_results_table.html",
            request,
            {
                **context,
                "company_profile": True,
                "listing_base_url": (
                    f"/company/{quote(context['company_type'], safe='')}/"
                    f"{quote(context['company_number'], safe='')}"
                ),
                "filter_form_id": "company-filter-form",
            },
        )
    return _render("partials/_company_detail_content.html", request, context)


__all__ = ["router"]
