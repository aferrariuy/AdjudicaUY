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
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app.database import get_db
from app.services.adjudication_service import (
    count_adjudications,
    distinct_organisms,
    filters_from_query_params,
    list_adjudications,
    ranking_by_company,
    temporal_by_organism,
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


def _build_temporal_chart_payload(
    rows: list[tuple[str, str, Decimal]],
) -> dict[str, Any]:
    """Shape temporal rows for a Chart.js multi-line chart.

    Input rows are flat ``(organism, month_iso, total)`` tuples. We
    pivot them into a ``{organism: {month: total}}`` map and then build
    parallel arrays of labels and per-organism datasets. The X-axis is
    the union of months that actually have data, in ascending order.

    See the temporal-visualization spec, "Time Granularity" requirement
    — monthly aggregation caps the data points at 12 per year.
    """

    by_organism: dict[str, dict[str, float]] = defaultdict(dict)
    months: set[str] = set()

    for organism, month_iso, total in rows:
        by_organism[organism][month_iso] = float(total)
        months.add(month_iso)

    sorted_months = sorted(months)

    palette = [
        "#1d4ed8",  # blue-700
        "#b91c1c",  # red-700
        "#15803d",  # green-700
        "#a16207",  # amber-700
        "#7c3aed",  # violet-600
        "#0e7490",  # cyan-700
        "#be185d",  # pink-700
        "#365314",  # lime-800
    ]

    datasets = []
    for index, organism in enumerate(sorted(by_organism)):
        data = [by_organism[organism].get(month, 0.0) for month in sorted_months]
        datasets.append(
            {
                "label": organism,
                "data": data,
                "borderColor": palette[index % len(palette)],
                "backgroundColor": palette[index % len(palette)],
                "tension": 0.2,
                "spanGaps": True,
            },
        )

    return {
        "type": "line",
        "labels": sorted_months,
        "datasets": datasets,
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

    filters = filters_from_query_params(dict(request.query_params))

    results = list_adjudications(db, filters, limit=PAGE_SIZE, offset=0)
    total = count_adjudications(db, filters)
    ranking_rows = ranking_by_company(db, filters, limit=RANKING_LIMIT)
    temporal_rows = temporal_by_organism(db, filters)
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
            "temporal_payload": _build_temporal_chart_payload(temporal_rows),
            "temporal_json": _json_dumps(_build_temporal_chart_payload(temporal_rows)),
            "organisms": organisms,
            "has_ranking_data": bool(ranking_rows),
            "has_temporal_data": bool(temporal_rows),
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
    filters = filters_from_query_params(dict(request.query_params))

    results = list_adjudications(db, filters, limit=PAGE_SIZE, offset=0)
    total = count_adjudications(db, filters)
    ranking_rows = ranking_by_company(db, filters, limit=RANKING_LIMIT)
    temporal_rows = temporal_by_organism(db, filters)

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
            "temporal_payload": _build_temporal_chart_payload(temporal_rows),
            "temporal_json": _json_dumps(_build_temporal_chart_payload(temporal_rows)),
            "has_ranking_data": bool(ranking_rows),
            "has_temporal_data": bool(temporal_rows),
        },
    )


__all__ = ["router"]
