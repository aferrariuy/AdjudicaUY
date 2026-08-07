"""Pure view-model presenters for charts, SEO metadata, and pagination."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.config import get_settings

if TYPE_CHECKING:
    from decimal import Decimal

    from app.services.dashboard import ConcentrationResult


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
    "_build_concentration_chart_payload",
    "_build_page_numbers",
    "_build_seo_context",
    "_build_trend_chart_payload",
]
