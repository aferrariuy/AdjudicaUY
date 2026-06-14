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

import json
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app.database import get_db
from app.services.adjudication_service import (
    DateValidationError,
    count_adjudications,
    distinct_organisms,
    filters_from_query_params,
    list_adjudications,
    ranking_by_company,
    ranking_by_organism,
    validate_date_params,
)

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
PAGE_SIZE = 50
RANKING_LIMIT = 10
ORGANISM_SUGGEST_LIMIT = 200

# Inline error fragment for 422 responses. Kept as a constant so both
# routes share the exact same markup; the message is injected as plain
# text (the validator already produces Spanish strings).
_ERROR_FRAGMENT_TEMPLATE = (
    '<div class="bg-red-50 border border-red-300 rounded-lg p-4" role="alert">'
    '<p class="text-red-800 text-sm">{message}</p>'
    '</div>'
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
    """Build a 422 response with the inline error fragment."""

    return HTMLResponse(
        _ERROR_FRAGMENT_TEMPLATE.format(message=message),
        status_code=422,
    )


# JSON encoder for Decimal — Chart.js expects plain numbers, not
# ``Decimal('1.250.000')`` strings.
class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:  # noqa: D401 - JSON encoder hook
        if isinstance(obj, Decimal):
            # ``float`` loses precision for values > 2^53 (~9 quadrillion
            # UYU) — not a concern for the chart's axis, which already
            # formats the number in the user's locale.
            return float(obj)
        if isinstance(obj, date):
            return obj.isoformat()
        return super().default(obj)


def _json_dumps(value: Any) -> str:
    """Serialize ``value`` for use as a ``data-*`` attribute payload."""

    return json.dumps(value, cls=_DecimalEncoder, separators=(",", ":"))


# ---------------------------------------------------------------------------
# View-model builders
# ---------------------------------------------------------------------------


def _build_ranking_chart_payload(
    rows: list[tuple[str, Decimal]],
) -> dict[str, Any]:
    """Shape ranking rows for a Chart.js bar chart.

    Returns a dict ready to be ``json.dumps``-ed into ``data-chart``:

    * ``labels`` — company names, in the order returned by the service
      (already sorted descending by total).
    * ``datasets[0].data`` — the totals, parallel to ``labels``.
    * ``format`` — a hint for the client-side tooltip/locale formatter.

    See the ranking-visualization spec, "Amount Formatting" scenarios
    for the ``es-UY`` locale expectation.
    """

    return {
        "type": "bar",
        "labels": [company for company, _total in rows],
        "datasets": [
            {
                "label": "Total adjudicado (UYU)",
                "data": [float(total) for _company, total in rows],
            },
        ],
        "format": {
            "locale": "es-UY",
            "currency": "UYU",
        },
    }


def _build_organism_ranking_payload(
    rows: list[tuple[str, Decimal]],
) -> dict[str, Any]:
    """Shape organism ranking rows for a Chart.js bar chart.

    Returns a dict ready to be ``json.dumps``-ed into ``data-chart``:

    * ``labels`` — organism names, in the order returned by the service
      (already sorted descending by total).
    * ``datasets[0].data`` — the totals, parallel to ``labels``.
    * ``format`` — a hint for the client-side tooltip/locale formatter.

    See the organism-ranking-visualization spec, "Payload structure
    matches company ranking" scenario for the ``es-UY`` locale
    expectation.
    """

    return {
        "type": "bar",
        "labels": [organism for organism, _total in rows],
        "datasets": [
            {
                "label": "Total adjudicado (UYU)",
                "data": [float(total) for _organism, total in rows],
            },
        ],
        "format": {
            "locale": "es-UY",
            "currency": "UYU",
        },
    }


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _render(
    template_name: str, request: Request, context: dict[str, Any]
) -> HTMLResponse:
    """Render ``template_name`` with ``context`` using the app's Jinja env."""

    templates = request.app.state.templates
    template = templates.get_template(template_name)
    return HTMLResponse(template.render({**context, "request": request}))


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Render the full index page.

    On the first GET (no filters in the query string), this returns the
    same data the ``/adjudications`` partial would return so the page is
    useful on cold load. The cost of a no-filter COUNT + 50-row scan is
    negligible at the data volumes the scraper produces.
    """

    params = cast("dict[str, str | None]", dict(request.query_params))
    _inject_default_year_params(params)
    try:
        validate_date_params(params)
    except DateValidationError as exc:
        return _validation_error_response(exc.message)
    filters = filters_from_query_params(params)

    results = list_adjudications(db, filters, limit=PAGE_SIZE, offset=0)
    total = count_adjudications(db, filters)
    ranking_rows = ranking_by_company(db, filters, limit=RANKING_LIMIT)
    organism_rows = ranking_by_organism(db, filters, limit=RANKING_LIMIT)
    organisms = distinct_organisms(db, filters, limit=ORGANISM_SUGGEST_LIMIT)

    return _render(
        "index.html",
        request,
        {
            "filters": filters,
            "results": results,
            "total": total,
            "shown": len(results),
            "page_size": PAGE_SIZE,
            "ranking_payload": _build_ranking_chart_payload(ranking_rows),
            "ranking_json": _json_dumps(_build_ranking_chart_payload(ranking_rows)),
            "organism_ranking_payload": _build_organism_ranking_payload(organism_rows),
            "organism_ranking_json": _json_dumps(
                _build_organism_ranking_payload(organism_rows)
            ),
            "organisms": organisms,
            "has_ranking_data": bool(ranking_rows),
            "has_organism_ranking_data": bool(organism_rows),
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
    except DateValidationError as exc:
        return _validation_error_response(exc.message)
    filters = filters_from_query_params(params)

    results = list_adjudications(db, filters, limit=PAGE_SIZE, offset=0)
    total = count_adjudications(db, filters)
    ranking_rows = ranking_by_company(db, filters, limit=RANKING_LIMIT)
    organism_rows = ranking_by_organism(db, filters, limit=RANKING_LIMIT)

    logger.info(
        "HTMX partial render: htmx=%s filters=%s total=%d",
        is_htmx,
        filters,
        total,
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
            "ranking_payload": _build_ranking_chart_payload(ranking_rows),
            "ranking_json": _json_dumps(_build_ranking_chart_payload(ranking_rows)),
            "organism_ranking_payload": _build_organism_ranking_payload(organism_rows),
            "organism_ranking_json": _json_dumps(
                _build_organism_ranking_payload(organism_rows)
            ),
            "has_ranking_data": bool(ranking_rows),
            "has_organism_ranking_data": bool(organism_rows),
        },
    )


__all__ = ["router"]
