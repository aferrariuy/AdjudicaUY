"""Pure view-model presenters for charts, SEO metadata, and pagination."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from app.config import get_settings

if TYPE_CHECKING:
    from decimal import Decimal

    from app.services.dashboard import ConcentrationResult


def _build_trend_chart_payload(
    rows: list[tuple[str, Decimal]],
    *,
    today: date | None = None,
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

    payload: dict[str, Any] = {
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
    if rows and rows[-1][0] == (today or date.today()).strftime("%Y-%m"):
        payload["partial"] = True
    return payload


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


_MIN_BREADCRUMB_ITEMS = 2


def _build_breadcrumb_json_ld(
    items: list[tuple[str, str | None]],
) -> dict[str, Any]:
    """Build a ``BreadcrumbList`` node for Google's breadcrumb rich result.

    Google requires ``name``, ``position`` and ``item`` on every ``ListItem``
    **except the last one**, where ``item`` may be omitted and the containing
    page's URL is used instead; it also requires at least two items, so a shorter
    trail is rejected here rather than published as markup that can never be
    eligible. Raising is deliberate: both call sites pass exactly two levels, so a
    shorter trail is a programming error rather than user input, and no request can
    produce one. Failing loudly is the cheaper failure — the alternative is publishing
    ineligible markup that nothing reports, which is how this docstring came to state
    a two-item rule the code did not enforce. Its
    guidelines recommend a trail that follows a real user path rather than
    mirroring the URL structure, which is why the trail here is
    "Inicio > <page>": an intermediate "Organismos" level would need a URL of
    its own, and this site has no such page, so inventing one would be invalid
    markup rather than a richer trail.
    """

    if len(items) < _MIN_BREADCRUMB_ITEMS:
        raise ValueError(
            "a BreadcrumbList needs at least "
            f"{_MIN_BREADCRUMB_ITEMS} items, got {len(items)}"
        )

    site_url = get_settings().site_url
    elements: list[dict[str, Any]] = []
    for position, (name, item_path) in enumerate(items, start=1):
        element: dict[str, Any] = {
            "@type": "ListItem",
            "position": position,
            "name": name,
        }
        if item_path is not None:
            element["item"] = f"{site_url}{item_path}"
        elements.append(element)

    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": elements,
    }


def _build_catalog_dataset_json_ld(
    *,
    date_span: tuple[date | None, date | None] | None = None,
) -> dict[str, Any]:
    """Build the ``Dataset`` node describing the whole catalogue.

    Google requires only ``name`` and ``description`` (50-5000 characters);
    everything else comes from its recommended list and is stated only where
    the project can back it up. One deliberate omission remains:

    * no ``license`` — the project declares none, and publishing terms it does
      not grant would misrepresent how the data may be reused.

    ``date_span`` is the catalogue's oldest and newest publication date, read
    from the same query that dates the sitemap, so the two can never disagree.
    It becomes ``temporalCoverage`` only when both ends are known: an unmeasured
    or half-open span stays unasserted, because a guessed span published as fact
    is worse than an absent property. The interval uses schema.org's ISO 8601
    ``start/end`` form.

    The ``distribution`` points at the CSV export. That endpoint answers
    ``X-Robots-Tag: noindex`` (it is a download, not a page), which is
    compatible: Dataset Search reads the markup on this landing page and
    follows ``contentUrl`` as a file link.
    """

    site_url = get_settings().site_url
    coverage: str | None = None
    if date_span is not None:
        start, end = date_span
        if start is not None and end is not None:
            coverage = f"{start.isoformat()}/{end.isoformat()}"

    node: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": "Adjudicaciones del Estado uruguayo",
        "description": (
            "Adjudicaciones publicadas por los organismos del Estado uruguayo, "
            "recopiladas de los reportes XML diarios de Compras Estatales y "
            "presentadas como un buscador público. Cada registro incluye el "
            "organismo comprador, la empresa adjudicataria, el artículo "
            "adjudicado, la cantidad, el monto en la moneda de origen con su "
            "equivalente en pesos uruguayos cuando la moneda es convertible, y "
            "la fecha de publicación de la adjudicación. Los datos se actualizan "
            "a diario."
        ),
        "url": f"{site_url}/",
        "creator": {
            "@type": "Organization",
            "name": "AdjudicaUY",
            "url": f"{site_url}/",
        },
        "isAccessibleForFree": True,
        "keywords": [
            "compras publicas",
            "contrataciones del Estado",
            "adjudicaciones",
            "transparencia",
            "gobierno abierto",
            "Uruguay",
        ],
        "spatialCoverage": {"@type": "Place", "name": "Uruguay"},
        "variableMeasured": [
            "Organismo comprador",
            "Empresa adjudicataria",
            "Articulo adjudicado",
            "Monto adjudicado en pesos uruguayos",
            "Fecha de publicacion de la adjudicacion",
        ],
        "distribution": [
            {
                "@type": "DataDownload",
                "encodingFormat": "text/csv",
                "contentUrl": f"{site_url}/adjudications/export",
            }
        ],
    }
    if coverage is not None:
        node["temporalCoverage"] = coverage
    return node


def _build_seo_context(
    *,
    meta_title: str,
    meta_description: str,
    og_type: str,
    path: str,
    breadcrumb: list[tuple[str, str | None]] | None = None,
    dataset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the SEO context dict passed to every full-page template.

    The dict provides page-specific values for the SEO blocks in
    ``base.html`` (meta description, OG tags, canonical URL). The
    ``canonical_url`` is built from ``settings.site_url`` + ``path``,
    stripping any query parameters so the canonical is stable. The
    ``og_image`` and ``og_site_name`` are shared across every page so
    social shares use the same branded card regardless of the route.

    ``breadcrumb`` and ``dataset`` are opt-in: a page that passes neither gets
    exactly the context it got before. When ``breadcrumb`` is given (as
    ``(name, path)`` pairs, the last one with ``path=None``) the context also
    carries the visible trail and its matching JSON-LD, both derived from the
    same list so the markup and the screen cannot drift apart.
    """

    settings = get_settings()
    canonical_url = f"{settings.site_url}{path}"
    context: dict[str, Any] = {
        "meta_title": meta_title,
        "meta_description": meta_description,
        "og_type": og_type,
        "canonical_url": canonical_url,
        "og_image": f"{settings.site_url}/static/og-image.png",
        "og_site_name": "AdjudicaUY",
    }
    if breadcrumb is not None:
        context["breadcrumb_items"] = [
            {"name": name, "href": item_path} for name, item_path in breadcrumb
        ]
        context["breadcrumb_json_ld"] = _build_breadcrumb_json_ld(breadcrumb)
    if dataset is not None:
        context["dataset_json_ld"] = dataset
    return context


@dataclass(frozen=True)
class EmptyWindowView:
    """View-model for the informative empty state of an entity page.

    Rendered when the active date window holds no adjudications. It carries
    the window that was actually queried (so the page can name it instead of
    saying "no data") and a link to the entity's last year with activity (so
    the visitor has a way out instead of a dead end).
    """

    window_label: str
    last_year: int | None
    last_year_url: str | None
    last_year_total_amount: Decimal | None
    last_year_purchase_count: int | None


def _build_empty_window_view(
    *,
    window_from: date,
    window_to: date,
    last_activity: date | None,
    last_year_total_amount: Decimal | None,
    last_year_purchase_count: int | None,
    path: str,
) -> EmptyWindowView:
    """Build the empty-window view-model for an entity page.

    ``path`` is the entity's own path (already URL-encoded) and the link
    deliberately targets the full page rather than the HTMX partial, so the
    filter form reloads showing the window that is actually displayed
    instead of keeping the old dates next to new data.
    """

    last_year = last_activity.year if last_activity is not None else None
    return EmptyWindowView(
        window_label=(
            f"{window_from.strftime('%d/%m/%Y')} \u2013 "
            f"{window_to.strftime('%d/%m/%Y')}"
        ),
        last_year=last_year,
        last_year_url=(
            f"{path}?date_from={last_year}-01-01&date_to={last_year}-12-31"
            if last_year is not None
            else None
        ),
        last_year_total_amount=last_year_total_amount,
        last_year_purchase_count=last_year_purchase_count,
    )


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


__all__ = [
    "EmptyWindowView",
    "_build_breadcrumb_json_ld",
    "_build_catalog_dataset_json_ld",
    "_build_concentration_chart_payload",
    "_build_empty_window_view",
    "_build_page_numbers",
    "_build_seo_context",
    "_build_trend_chart_payload",
]
