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
    ORGANISM_SUGGEST_LIMIT,
    PAGE_SIZE,
    RANKING_LIMIT,
    _coerce_page,
    _full_page_validation_error,
    _inject_default_year_params,
    _render,
    _stream_csv_response,
    _validation_error_response,
)
from app.services.company import (
    CompanyProfileSummary,
    CompanyWinRate,
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

    summary = CompanyProfileSummary(
        display_name=None,
        total_amount=Decimal("0"),
        total=0,
        purchase_count=0,
        organism_count=0,
        share_of_total=Decimal("0"),
    )
    identity_name: str | None = None
    if decoded_type and decoded_number:
        identity_name = lookup_company_identity(db, decoded_type, decoded_number)
        market_filters = replace(filters, company_doc_exact=None)
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

    page = max(_coerce_page(raw_params.get("page")), 1)
    total = 0 if not decoded_type or not decoded_number else summary.total
    total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total > 0 else 1
    results = (
        []
        if not decoded_type or not decoded_number
        else list_adjudications(
            db,
            filters,
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
        )
    )
    trend_rows = (
        []
        if not decoded_type or not decoded_number
        else cached_aggregate("monthly_trend", monthly_trend, db, filters)
    )
    concentration = (
        ConcentrationResult(None, 0, 0)
        if not decoded_type or not decoded_number
        else cached_aggregate("concentration_ratio", concentration_ratio, db, filters)
    )
    win_rate = (
        CompanyWinRate(0, 0, None)
        if not decoded_type or not decoded_number
        else cached_aggregate(
            "company_win_rate",
            lambda s, f: company_win_rate(s, decoded_type, decoded_number, f),
            db,
            filters,
        )
    )
    competitors = (
        []
        if not decoded_type or not decoded_number
        else cached_aggregate(
            "company_competitors",
            lambda s, f: company_competitors(s, decoded_type, decoded_number, f),
            db,
            filters,
        )
    )
    ranking_rows = (
        []
        if not decoded_type or not decoded_number
        else cached_aggregate(
            "ranking_by_organism",
            ranking_by_organism,
            db,
            filters,
            limit=RANKING_LIMIT,
        )
    )
    top_article_rows = (
        []
        if not decoded_type or not decoded_number
        else cached_aggregate(
            "top_articles",
            top_articles,
            db,
            filters,
            limit=RANKING_LIMIT,
        )
    )

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
        "page_numbers": _build_page_numbers(page, total_pages),
        "ranking_rows": ranking_rows,
        "top_article_rows": top_article_rows,
        "organisms": (
            []
            if not decoded_type or not decoded_number
            else cached_aggregate(
                "distinct_organisms",
                distinct_organisms,
                db,
                filters,
                limit=ORGANISM_SUGGEST_LIMIT,
            )
        ),
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
    raw_params = cast("dict[str, str | None]", dict(request.query_params))
    decoded_identity = (unquote(tipo_doc_prov), unquote(nro_doc_prov))
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
