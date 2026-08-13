"""Organism profile routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.presenters import (
    _build_concentration_chart_payload,
    _build_seo_context,
    _build_trend_chart_payload,
)
from app.routes._base import HeadAwareAPIRoute
from app.routes.common import (
    RANKING_LIMIT,
    _full_page_validation_error,
    _inject_default_year_params,
    _render,
    _validation_error_response,
)
from app.services.dashboard import (
    concentration_ratio,
    kpi_summary,
    monthly_trend,
    ranking_by_company,
)
from app.services.filters import (
    AdjudicationFilters,
    ValidationError,
    filters_from_query_params,
    validate_date_params,
)
from app.services.query_cache import cached_aggregate

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
router = APIRouter(route_class=HeadAwareAPIRoute)


def _build_organism_context(
    db: Session,
    *,
    decoded_name: str,
    raw_params: dict[str, str | None],
) -> dict[str, Any]:
    """Build the view-model shared by the full organism page and its partial.

    Both ``GET /organism/{name}`` and ``GET /organism/{name}/partial``
    share the same data shape: an exact-match organism filter combined
    with the user-supplied ``date_from`` / ``date_to`` / ``article`` /
    ``article_id`` filters. Keeping the assembly in one helper ensures
    the page and its HTMX refresh are guaranteed to render the same
    widgets against the same snapshot.

    When no date filters are provided (cold navigation from the main
    page which defaults to the current year), the current calendar year
    is injected so the organism profile stays consistent with the
    user's expectation of filtered results.
    """

    _inject_default_year_params(raw_params)
    validate_date_params(raw_params)
    # Parse the user-supplied secondary filters (date / article / id) but
    # override the organism slot with the exact name decoded from the
    # path. The service treats ``organism_exact`` as a strict equality
    # predicate — see ``_build_predicates`` in the service layer.
    filters = filters_from_query_params(raw_params)
    filters = AdjudicationFilters(
        company=filters.company,
        organism_exact=decoded_name,
        article=filters.article,
        article_id=filters.article_id,
        date_from=filters.date_from,
        date_to=filters.date_to,
    )

    kpi = cached_aggregate("kpi_summary", kpi_summary, db, filters)
    trend_rows = cached_aggregate("monthly_trend", monthly_trend, db, filters)
    concentration = cached_aggregate(
        "concentration_ratio", concentration_ratio, db, filters
    )
    concentration_payload = (
        _build_concentration_chart_payload(concentration)
        if concentration.ratio is not None
        else None
    )
    company_ranking = cached_aggregate(
        "ranking_by_company", ranking_by_company, db, filters, limit=RANKING_LIMIT
    )

    return {
        "filters": filters,
        "organism_name": decoded_name,
        "kpi": kpi,
        "trend_rows": trend_rows,
        # The dicts are serialized by Jinja's ``|tojson`` filter.
        "trend_payload": _build_trend_chart_payload(trend_rows),
        "has_trend_data": bool(trend_rows),
        "concentration": concentration,
        "concentration_payload": concentration_payload,
        "has_concentration_data": concentration.ratio is not None,
        "link_to_company": True,
        "company_variant": False,
        # The company ranking on the organism page reuses the same
        # variable names as the index page's ``_ranking_list.html``
        # partial — the partial reads ``ranking_rows`` regardless of
        # which route rendered it.
        "ranking_rows": company_ranking,
    }


@router.get(
    "/organism/{name}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def organism_detail(
    name: str,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Render the full organism profile page.

    FastAPI already URL-decodes the path segment (``Ministerio%20del%20Interior``
    arrives as ``"Ministerio del Interior"``), but the design requires
    an explicit ``urllib.parse.unquote`` step so a doubled encoding
    (e.g. ``%2520``) round-trips cleanly. The decoded name is then
    matched exactly against ``Compra.organismo`` so the route only
    shows rows for that organism — see the organism-profile spec,
    "Organism Profile Route" requirement.
    """

    decoded_name = unquote(name).strip()
    raw_params = cast("dict[str, str | None]", dict(request.query_params))
    try:
        context = _build_organism_context(
            db, decoded_name=decoded_name, raw_params=raw_params
        )
    except ValidationError as exc:
        return _full_page_validation_error(
            "organism_detail.html",
            request,
            exc.message,
            raw_params=raw_params,
            organism_name=decoded_name,
        )

    seo = _build_seo_context(
        meta_title=f"{decoded_name} — AdjudicaUY",
        meta_description=(
            f"Adjudicaciones del organismo {decoded_name} en el Estado uruguayo."
        ),
        og_type="GovernmentOrganization",
        path=f"/organism/{quote(decoded_name, safe='')}",
    )
    context.update(seo)
    return _render("organism_detail.html", request, context)


@router.get(
    "/organism/{name}/partial",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def organism_detail_partial(
    name: str,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Render the HTMX-swappable body of the organism profile.

    The form on the organism page targets this endpoint, and the
    response replaces only the body container — the page chrome
    (header, footer, filter form) is NOT re-rendered. The shape of
    the body is identical to the body section of ``organism_detail.html``
    (KPI cards, trend, concentration, company ranking), so the user
    sees the dashboard widgets refresh in place when a filter
    changes.
    """

    decoded_name = unquote(name).strip()
    raw_params = cast("dict[str, str | None]", dict(request.query_params))
    try:
        context = _build_organism_context(
            db, decoded_name=decoded_name, raw_params=raw_params
        )
    except ValidationError as exc:
        return _validation_error_response(exc.message)

    logger.info(
        "Organism partial render: organism=%s filters=%s",
        decoded_name,
        context["filters"],
    )
    return _render("partials/_organism_detail_content.html", request, context)


__all__ = ["router"]
