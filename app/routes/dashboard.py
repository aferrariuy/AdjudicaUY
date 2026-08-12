"""Dashboard listing and CSV export routes."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, cast

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
from app.services.dashboard import (
    concentration_ratio,
    distinct_organisms,
    kpi_summary,
    monthly_trend,
    ranking_by_company,
    ranking_by_organism,
)
from app.services.filters import (
    ValidationError,
    filters_from_query_params,
    validate_date_params,
)
from app.services.listing import count_adjudications, list_adjudications
from app.services.query_cache import cached_aggregate

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
router = APIRouter(route_class=HeadAwareAPIRoute)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request, db: Session = Depends(get_db)) -> Response:
    """Render the full index page.

    On the first GET (no filters in the query string), this returns the
    same data the ``/adjudications`` partial would return so the page is
    useful on cold load. The KPI aggregate plus 50-row scan is kept bounded
    and cached between identical dashboard requests.

    The return type is ``Response`` (not ``HTMLResponse``) because the
    pagination spec requires a 302 ``RedirectResponse`` for
    out-of-bounds page requests (``adjudication-pagination`` spec,
    "Out-of-Bounds Redirect" requirement).
    """

    params = cast("dict[str, str | None]", dict(request.query_params))
    _inject_default_year_params(params)
    try:
        validate_date_params(params)
    except ValidationError as exc:
        return _full_page_validation_error(
            "index.html", request, exc.message, raw_params=params
        )
    filters = filters_from_query_params(params)

    # KPI total is computed before pagination so out-of-bounds requests can
    # redirect without a separate count query.
    kpi = cached_aggregate("kpi_summary", kpi_summary, db, filters)
    total = kpi.total

    # Pagination. The ``page`` param is parsed manually so missing /
    # empty / non-integer / negative values all collapse to page 1
    # (the spec forbids a 4xx for malformed input).
    page = max(_coerce_page(params.get("page")), 1)
    offset = (page - 1) * PAGE_SIZE

    # ``total_pages`` is at least 1 even when the result
    # set is empty — the empty-state branch uses ``total == 0`` to
    # show the "no results" panel and the pagination bar is gated on
    # ``total_pages > 1`` in the template.
    total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total > 0 else 1
    if page > total_pages and total > 0:
        # Out-of-bounds: land the user on the last valid page so they
        # see real data instead of an empty slice. The relative URL
        # preserves the current path (``/``) and the surrounding
        # browser state.
        return RedirectResponse(url=f"?page={total_pages}", status_code=302)

    results = list_adjudications(db, filters, limit=PAGE_SIZE, offset=offset)
    page_numbers = _build_page_numbers(page, total_pages)
    ranking_rows = cached_aggregate(
        "ranking_by_company", ranking_by_company, db, filters, limit=RANKING_LIMIT
    )
    organism_rows = cached_aggregate(
        "ranking_by_organism", ranking_by_organism, db, filters, limit=RANKING_LIMIT
    )
    organisms = cached_aggregate(
        "distinct_organisms",
        distinct_organisms,
        db,
        filters,
        limit=ORGANISM_SUGGEST_LIMIT,
    )

    # Citizen-dashboard aggregates (PR#1). Each one honours the same
    # filter set as the listing so the KPI / trend / concentration
    # numbers are consistent with what the user sees in the table.
    # The aggregates are NOT paginated — they reflect the full
    # filtered set, not the current page slice (adjudication-
    # pagination spec, "Dashboard Aggregates Use Full Filtered Set").
    trend_rows = cached_aggregate("monthly_trend", monthly_trend, db, filters)
    concentration = cached_aggregate(
        "concentration_ratio", concentration_ratio, db, filters
    )
    concentration_payload = (
        _build_concentration_chart_payload(concentration)
        if concentration.ratio is not None
        else None
    )

    return _render(
        "index.html",
        request,
        {
            **_build_seo_context(
                meta_title="AdjudicaUY",
                meta_description=(
                    "Buscador de adjudicaciones del Estado uruguayo. "
                    "Filtrá por organismo, empresa, artículo y fecha."
                ),
                og_type="website",
                path="/",
            ),
            "filters": filters,
            "results": results,
            "total": total,
            "shown": len(results),
            "page_size": PAGE_SIZE,
            "page": page,
            "total_pages": total_pages,
            "page_numbers": page_numbers,
            "ranking_rows": ranking_rows,
            "organism_rows": organism_rows,
            "link_to_company": True,
            "company_variant": False,
            "organisms": organisms,
            # Citizen-dashboard payloads. The dicts are serialized by
            # Jinja's ``|tojson`` filter in the templates, which escapes
            # for safe use inside HTML attributes.
            "kpi": kpi,
            "trend_rows": trend_rows,
            "trend_payload": _build_trend_chart_payload(trend_rows),
            "has_trend_data": bool(trend_rows),
            "concentration": concentration,
            "concentration_payload": concentration_payload,
            "has_concentration_data": concentration.ratio is not None,
        },
    )


@router.get("/adjudications", response_class=HTMLResponse, include_in_schema=False)
def adjudications_partial(request: Request, db: Session = Depends(get_db)) -> Response:
    """Render the HTMX partial returned into the ``#results`` container.

    HTMX signals the request is coming from a swap via the
    ``HX-Request`` header. The response body is the same in both cases
    — the caller (the browser) does the swapping — but logging the
    distinction helps when tracing a misbehaving partial.
    """

    is_htmx = request.headers.get("HX-Request") == "true"
    params = cast("dict[str, str | None]", dict(request.query_params))
    _inject_default_year_params(params)
    try:
        validate_date_params(params)
    except ValidationError as exc:
        return _validation_error_response(exc.message)
    filters = filters_from_query_params(params)

    # When ``partial=table`` is present, the request comes from a
    # pagination link that only needs the table + pagination bar.
    # We skip the expensive aggregate queries (rankings, KPI, trend,
    # concentration) since they don't change between pages.
    table_only = params.get("partial") == "table"

    if table_only:
        # Pagination — see the index route for the parsing rationale.
        page = max(_coerce_page(params.get("page")), 1)
        offset = (page - 1) * PAGE_SIZE
        total = count_adjudications(db, filters)
    else:
        # Full responses use the KPI's full-set row count, avoiding a
        # separate count query for the same filters.
        kpi = cached_aggregate("kpi_summary", kpi_summary, db, filters)
        total = kpi.total
        page = max(_coerce_page(params.get("page")), 1)
        offset = (page - 1) * PAGE_SIZE

    total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total > 0 else 1
    if page > total_pages and total > 0:
        return RedirectResponse(url=f"?page={total_pages}", status_code=302)

    results = list_adjudications(db, filters, limit=PAGE_SIZE, offset=offset)
    page_numbers = _build_page_numbers(page, total_pages)

    if table_only:
        # Lightweight response: only the table + pagination bar.
        logger.info(
            "HTMX partial render (table-only): filters=%s total=%d page=%d/%d",
            filters,
            total,
            page,
            total_pages,
        )
        return _render(
            "partials/_results_table.html",
            request,
            {
                "filters": filters,
                "results": results,
                "total": total,
                "shown": len(results),
                "page_size": PAGE_SIZE,
                "page": page,
                "total_pages": total_pages,
                "page_numbers": page_numbers,
            },
        )

    # Full response: table + all dashboard aggregates.
    ranking_rows = cached_aggregate(
        "ranking_by_company", ranking_by_company, db, filters, limit=RANKING_LIMIT
    )
    organism_rows = cached_aggregate(
        "ranking_by_organism", ranking_by_organism, db, filters, limit=RANKING_LIMIT
    )

    # Citizen-dashboard aggregates (PR#1). See the index route for the
    # rationale on the empty-state payload rule for concentration.
    # Aggregates are NOT paginated — they reflect the full filtered
    # set, not the current page slice.
    trend_rows = cached_aggregate("monthly_trend", monthly_trend, db, filters)
    concentration = cached_aggregate(
        "concentration_ratio", concentration_ratio, db, filters
    )
    concentration_payload = (
        _build_concentration_chart_payload(concentration)
        if concentration.ratio is not None
        else None
    )

    logger.info(
        "HTMX partial render: htmx=%s filters=%s total=%d page=%d/%d",
        is_htmx,
        filters,
        total,
        page,
        total_pages,
    )

    return _render(
        "partials/_results.html",
        request,
        {
            "filters": filters,
            "results": results,
            "total": total,
            "shown": len(results),
            "page_size": PAGE_SIZE,
            "page": page,
            "total_pages": total_pages,
            "page_numbers": page_numbers,
            "ranking_rows": ranking_rows,
            "organism_rows": organism_rows,
            "link_to_company": True,
            "company_variant": False,
            # Citizen-dashboard payloads. The dicts are serialized by
            # Jinja's ``|tojson`` filter in the templates, which escapes
            # for safe use inside HTML attributes.
            "kpi": kpi,
            "trend_rows": trend_rows,
            "trend_payload": _build_trend_chart_payload(trend_rows),
            "has_trend_data": bool(trend_rows),
            "concentration": concentration,
            "concentration_payload": concentration_payload,
            "has_concentration_data": concentration.ratio is not None,
        },
    )


@router.get("/adjudications/export", include_in_schema=False)
def export_adjudications(request: Request) -> Response:
    """Stream a CSV of adjudications matching the active filters."""

    params = cast("dict[str, str | None]", dict(request.query_params))
    _inject_default_year_params(params)
    try:
        validate_date_params(params)
    except ValidationError as exc:
        return Response(exc.message, status_code=422, media_type="text/plain")

    return _stream_csv_response(filters_from_query_params(params))


__all__ = ["router"]
