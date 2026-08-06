"""HTTP routes for the AdjudicaUY web application.

Two routes are exposed:

* ``GET /`` — full HTML page (base layout + index). Used for direct
  navigation; HTMX never hits it.
* ``GET /adjudications`` — HTMX partial. Returns the results table
  fragment; the browser replaces only the ``#results`` element. The two
  chart partials are also rendered in the response so the page can
  re-attach the ``data-chart`` payloads after the swap (see
  ``base.html`` for the ``htmx:afterSwap`` listener).

The route layer is intentionally thin: it parses query parameters,
delegates the heavy lifting to :mod:`app.services.adjudication_service`,
and renders Jinja2 templates. No SQLAlchemy, no business logic.
"""

from __future__ import annotations

import csv
import io
import logging
import math
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from markupsafe import escape

from app.config import get_settings
from app.database import get_db, get_session_factory
from app.services.adjudication_service import (
    MAX_EXPORT_ROWS,
    AdjudicationFilters,
    CompanyProfileSummary,
    CompanyWinRate,
    ConcentrationResult,
    ValidationError,
    company_competitors,
    company_summary,
    company_win_rate,
    concentration_ratio,
    count_adjudications,
    distinct_organisms,
    filters_from_query_params,
    iter_adjudications,
    kpi_summary,
    list_adjudications,
    lookup_company_identity,
    monthly_trend,
    ranking_by_company,
    ranking_by_organism,
    top_articles,
    validate_date_params,
)
from app.services.query_cache import cached_aggregate

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Router + template paths
# ---------------------------------------------------------------------------

router = APIRouter()

# Templates live under ``app/templates``. The path is resolved at import
# time so the route module is self-contained — the app factory in
# ``app.main`` wires these onto ``request.app.state``.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
PARTIALS_DIR = TEMPLATES_DIR / "partials"

# Pagination bounds. The route caps user-supplied values implicitly by
# ignoring them; we expose fixed constants so the UI can render the same
# numbers.
PAGE_SIZE = 10
RANKING_LIMIT = 10
ORGANISM_SUGGEST_LIMIT = 200

# Inline error fragment for 422 responses. Kept as a constant so both
# routes share the exact same markup; the message is injected as plain
# text (the validator already produces Spanish strings).
_ERROR_FRAGMENT_TEMPLATE = (
    '<div class="bg-red-50 border border-red-300 rounded-lg p-4" role="alert">'
    '<p class="text-red-800 text-sm">{message}</p>'
    "</div>"
)


def _inject_default_year_params(params: dict[str, str | None]) -> None:
    """Default the date range to the current calendar year when absent.

    Mutates ``params`` in place. Only fires when BOTH ``date_from`` and
    ``date_to`` are missing or empty — a single explicit param is left
    alone (see the spec, "Only one date param provided — no default
    injection" scenario).
    """

    if not params.get("date_from") and not params.get("date_to"):
        today = date.today()
        params["date_from"] = f"{today.year}-01-01"
        params["date_to"] = f"{today.year}-12-31"


def _validation_error_response(message: str) -> HTMLResponse:
    """Build a 422 response with the inline error fragment.

    The message is escaped before interpolation so that any future code
    path that passes user input here cannot inject HTML/JS into the
    fragment.
    """

    return HTMLResponse(
        _ERROR_FRAGMENT_TEMPLATE.format(message=escape(message)),
        status_code=422,
    )


def _validation_error_context(
    message: str,
    *,
    raw_params: dict[str, str | None] | None = None,
    organism_name: str | None = None,
    company_identity: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Build the minimal context for a full-page 422 render.

    The full-page routes (``GET /`` and ``GET /organism/{name}``) catch
    ``ValidationError`` *before* running any DB queries, so the
    template only needs the bare minimum to render the chrome (filter
    form + error fragment) without raising ``UndefinedError`` on
    ``filters`` / ``organisms`` / etc. The error is stored in
    ``validation_error``; both ``index.html`` and
    ``organism_detail.html`` look for that key to swap the
    ``#results`` / ``#organism-body`` body for the alert fragment.

    Parameters
    ----------
    message
        Spanish error string from :class:`ValidationError`.
    raw_params
        Echoed back into ``filters`` so the filter form preserves the
        user's input (``date_from``, ``date_to``, etc.) when the page
        reloads with the error — the user can correct the date and
        resubmit without retyping. Defaults to an empty dict.
    organism_name
        Required only for ``organism_detail.html`` so the ``<h1>`` and
        ``<title>`` render the requested organism even on the error
        path. ``index.html`` ignores it.
    """

    params = raw_params if raw_params is not None else {}
    filters = filters_from_query_params(params)
    context: dict[str, Any] = {
        "filters": filters,
        "organisms": [],
        "validation_error": message,
    }
    if organism_name is not None:
        context["organism_name"] = organism_name
    if company_identity is not None:
        context["company_type"], context["company_number"] = company_identity
        context["company_name"] = None
    return context


def _full_page_validation_error(
    template_name: str,
    request: Request,
    message: str,
    *,
    raw_params: dict[str, str | None] | None = None,
    organism_name: str | None = None,
    company_identity: tuple[str, str] | None = None,
) -> HTMLResponse:
    """Build a full-page 422 response: render ``template_name`` with the error.

    Full-page counterpart of :func:`_validation_error_response`. The
    HTMX partials use that helper (the page chrome is already on screen
    and HTMX swaps the fragment into ``#results`` / ``#organism-body``);
    the full-page routes use this one so a direct navigation to
    ``GET /`` or ``GET /organism/{name}`` renders the page layout
    around the error.
    """

    return HTMLResponse(
        _render_str(
            template_name,
            request,
            _validation_error_context(
                message,
                raw_params=raw_params,
                organism_name=organism_name,
                company_identity=company_identity,
            ),
        ),
        status_code=422,
    )


# ---------------------------------------------------------------------------
# View-model builders
# ---------------------------------------------------------------------------


def _build_trend_chart_payload(
    rows: list[tuple[str, Decimal]],
) -> dict[str, Any]:
    """Shape monthly trend rows for a Chart.js line/area chart.

    The service already returns the labels in chronological order
    and fills in sparse months with ``Decimal(0)``; we just project
    them to the data Chart.js consumes (see the temporal-trend
    spec, "Chart renders with multi-month data" scenario).

    * ``type`` — ``"line"`` (with ``fill: true`` so the area below
      the line is shaded, giving the "area chart" visual the spec
      calls for).
    * ``labels`` — ``YYYY-MM`` strings, chronological.
    * ``datasets[0].data`` — totals per month, parallel to labels.
    * ``format`` — ``es-UY`` UYU currency, consistent with the other
      charts on the page.
    """

    return {
        "type": "line",
        "labels": [label for label, _total in rows],
        "datasets": [
            {
                "label": "Total adjudicado (UYU)",
                "data": [float(total) for _label, total in rows],
                "fill": True,
                "borderColor": "#1B2A4A",
                "backgroundColor": "rgba(27, 42, 74, 0.1)",
                "tension": 0.1,
            },
        ],
        "format": {
            "locale": "es-UY",
            "currency": "UYU",
        },
    }


def _build_concentration_chart_payload(
    result: ConcentrationResult,
    *,
    competition_labels: bool = False,
) -> dict[str, Any]:
    """Shape the market-concentration metric for a Chart.js doughnut.

    Two segments — "1 oferente" (single bidder) and ">1 oferentes"
    (multi bidder). Purchases with zero oferentes are excluded from
    both, so the segments always sum to the total compras that
    received at least one bid. The ``format`` hint carries the
    ``es-UY`` percentage locale so the donut tooltip can format
    share values per the market-concentration spec, "Percentage
    formatting" scenario.

    The route only invokes this builder when ``result.ratio`` is
    not ``None`` (denominator > 0); the empty state is rendered
    separately by the partial.
    """

    labels = (
        ["sin competencia", "con competencia"]
        if competition_labels
        else ["1 oferente", "más de 1 oferente"]
    )

    return {
        "type": "doughnut",
        "labels": labels,
        "datasets": [
            {
                "label": "Compras por oferentes",
                "data": [
                    result.single_bidder_count,
                    result.multi_bidder_count,
                ],
                "backgroundColor": ["#B23B2E", "#1B2A4A"],
            },
        ],
        "format": {
            "locale": "es-UY",
            "percentage": True,
        },
    }


# ---------------------------------------------------------------------------
# Pagination helpers
# ---------------------------------------------------------------------------


def _coerce_page(raw: str | None) -> int:
    """Parse a raw ``page`` query param, returning 1 for missing/garbage values.

    The route never returns a 4xx for a malformed ``page`` value — we
    silently fall back to the first page. The caller is responsible for
    clamping to ``>= 1`` (negative values map to 1 too, but a positive
    integer stays as-is at this layer so the caller can distinguish
    ``page=0`` from ``page=1`` if it wants to).
    """

    if raw is None:
        return 1
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        return 1


def _build_page_numbers(current: int, total: int) -> list[int | str]:
    """Return the visible page numbers + ellipsis markers for the pagination bar.

    The list is at most 7 entries long. The first (1) and last (total)
    pages are always present; the current page is always present and
    centered when the window is truncated. ``"…"`` (ellipsis) entries
    mark skipped ranges. With 7 or fewer pages, all numbers are shown
    with no truncation.
    """

    if total <= 7:
        return list(range(1, total + 1))

    # Three middle slots around the current page (``current ± 1``); the
    # two edges (1, total) are always added separately. The total entry
    # count is 7 = 1 + 3 + 1 + 1 + 1 (edges + middle + two possible
    # ellipsis markers).
    half = 1
    start = max(2, current - half)
    end = min(total - 1, current + half)
    # Push the window away from an edge when it would be squashed, so
    # the current page still has a neighbor on the inside.
    if end - start < 2 * half:
        if start == 2:
            end = min(total - 1, start + 2 * half)
        elif end == total - 1:
            start = max(2, end - 2 * half)

    pages: list[int | str] = [1]
    if start > 2:
        pages.append("…")
    pages.extend(range(start, end + 1))
    if end < total - 1:
        pages.append("…")
    pages.append(total)
    return pages


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _render_str(template_name: str, request: Request, context: dict[str, Any]) -> str:
    """Render ``template_name`` with ``context`` to an HTML string.

    Used by routes that need to set a non-default status code on the
    response (e.g. the full-page 422 path). The default-status variant
    :func:`_render` wraps this in an :class:`HTMLResponse`.
    """

    templates = request.app.state.templates
    template = templates.get_template(template_name)
    return template.render({**context, "request": request})


def _render(
    template_name: str, request: Request, context: dict[str, Any]
) -> HTMLResponse:
    """Render ``template_name`` with ``context`` using the app's Jinja env."""

    return HTMLResponse(_render_str(template_name, request, context))


def _build_seo_context(
    *,
    meta_title: str,
    meta_description: str,
    og_type: str,
    path: str,
) -> dict[str, Any]:
    """Build the SEO context dict passed to every full-page template.

    The dict provides page-specific values for the SEO blocks in
    ``base.html`` (meta description, OG tags, canonical URL). The
    ``canonical_url`` is built from ``settings.site_url`` + ``path``,
    stripping any query parameters so the canonical is stable.
    """

    settings = get_settings()
    canonical_url = f"{settings.site_url}{path}"
    return {
        "meta_title": meta_title,
        "meta_description": meta_description,
        "og_type": og_type,
        "canonical_url": canonical_url,
    }


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


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

# CSV column order per the spec.
_CSV_COLUMNS = [
    "fecha",
    "organismo",
    "empresa_adjudicataria",
    "articulo",
    "monto",
    "moneda",
    "monto_uyu",
    "tipo_compra",
    "documento_empresa",
    "tipo_documento",
    "id_articulo",
]


def _row_to_csv_fields(row: Any) -> list[str]:
    """Map an :class:`AdjudicationRow` to the CSV column values.

    Raw values only — no es-UY formatting. Dates are ISO, decimals are
    unformatted, NULLs become empty strings.
    """

    return [
        row.date.isoformat(),
        row.organism or "",
        row.winning_company or "",
        row.article or "",
        str(row.amount) if row.amount is not None else "",
        row.currency or "",
        str(row.amount_uyu) if row.amount_uyu is not None else "",
        row.license_type or "",
        row.company_document or "",
        row.company_document_type or "",
        row.article_id or "",
    ]


def _stream_csv_response(filters: AdjudicationFilters) -> Response:
    """Stream the standard CSV response for a prepared filter set.

    The generator owns a manual session because ``StreamingResponse`` begins
    iterating after the route returns. Keeping this shared by the global and
    company exports guarantees identical columns, limits, and lifecycle.
    """

    # Manual session — the generator closes it.
    session = get_session_factory()()
    try:
        total = count_adjudications(session, filters)
    except Exception:
        session.close()
        raise

    if total > MAX_EXPORT_ROWS:
        session.close()
        return Response(
            "La exportación supera el límite de 500.000 filas. Ajuste los filtros.",
            status_code=400,
            media_type="text/plain",
        )

    def _csv_generator() -> Any:
        """Yield CSV bytes: BOM → header → data rows."""

        try:
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\r\n")

            # UTF-8 BOM for Excel compatibility.
            yield "\ufeff"

            # Header row.
            writer.writerow(_CSV_COLUMNS)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

            # Data rows.
            for row in iter_adjudications(session, filters):
                writer.writerow(_row_to_csv_fields(row))
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
        finally:
            session.close()

    return StreamingResponse(
        _csv_generator(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="adjudicaciones.csv"',
        },
    )


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


# ---------------------------------------------------------------------------
# Organism profile (PR#2 of citizen-dashboard)
# ---------------------------------------------------------------------------


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

    kpi = kpi_summary(db, filters)
    trend_rows = monthly_trend(db, filters)
    concentration = concentration_ratio(db, filters)
    concentration_payload = (
        _build_concentration_chart_payload(concentration)
        if concentration.ratio is not None
        else None
    )
    company_ranking = ranking_by_company(db, filters, limit=RANKING_LIMIT)

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


# ---------------------------------------------------------------------------
# Company profile (company document identity)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# About page (informational)
# ---------------------------------------------------------------------------


@router.get("/about", response_class=HTMLResponse, include_in_schema=False)
def about(request: Request) -> HTMLResponse:
    """Render the informational about page with SEO context."""

    seo = _build_seo_context(
        meta_title="Sobre AdjudicaUY",
        meta_description=(
            "AdjudicaUY es una plataforma de búsqueda y visualización "
            "de adjudicaciones del Estado uruguayo. Datos públicos, "
            "abiertos y actualizados."
        ),
        og_type="website",
        path="/about",
    )
    return _render("pages/about.html", request, seo)


__all__ = ["router"]
