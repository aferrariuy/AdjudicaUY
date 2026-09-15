"""Unit tests for the pure view-shaping presenters (charts, SEO context)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.presenters import _build_seo_context, _build_trend_chart_payload


def test_trend_payload_marks_current_month_without_dropping_points() -> None:
    rows = [
        ("2026-07", Decimal("100")),
        ("2026-08", Decimal("25")),
    ]

    payload = _build_trend_chart_payload(rows, today=date(2026, 8, 7))

    assert payload["partial"] is True
    assert payload["labels"] == ["2026-07", "2026-08"]
    assert payload["datasets"][0]["data"] == [100.0, 25.0]


def test_trend_payload_omits_partial_for_historical_final_month() -> None:
    rows = [
        ("2026-06", Decimal("100")),
        ("2026-07", Decimal("25")),
    ]

    payload = _build_trend_chart_payload(rows, today=date(2026, 8, 7))

    assert "partial" not in payload
    assert len(payload["labels"]) == len(payload["datasets"][0]["data"]) == 2


def test_seo_context_has_no_double_slash_when_site_url_has_trailing_slash(
    monkeypatch: Any,
) -> None:
    """A trailing-slash SITE_URL must not produce ``//`` in absolute URLs."""

    monkeypatch.setenv("SITE_URL", "https://adjudicauy.appuy.dedyn.io/")

    context = _build_seo_context(
        meta_title="AdjudicaUY",
        meta_description="Adjudicaciones del Estado uruguayo",
        og_type="website",
        path="/",
    )

    assert context["canonical_url"] == "https://adjudicauy.appuy.dedyn.io/"
    assert context["og_image"] == (
        "https://adjudicauy.appuy.dedyn.io/static/og-image.png"
    )


def test_seo_context_offers_the_lowercase_host_as_an_alternate_site_name(
    monkeypatch: Any,
) -> None:
    """The context names the site's own host so Google has a fallback name.

    When Google does not select a site's preferred name it may show the
    domain-level name instead, and its documented remedy is ``alternateName``
    with the host in all lowercase. This host sits under deSEC's shared
    ``dedyn.io`` dynamic-DNS namespace, whose parent resolves to a different
    product, so the name Google substitutes is a foreign brand rather than a
    near miss of this one.

    The name must come from the same ``site_url`` that builds ``canonical_url``:
    an alternate name for a host the site does not publish would be worse than
    none, so the two values are asserted together here.
    """

    monkeypatch.setenv("SITE_URL", "https://AdjudicaUY.AppUY.dedyn.io/")

    context = _build_seo_context(
        meta_title="AdjudicaUY",
        meta_description="Adjudicaciones del Estado uruguayo",
        og_type="website",
        path="/",
    )

    assert context["alternate_site_names"] == ["adjudicauy.appuy.dedyn.io"]
    assert context["canonical_url"] == "https://AdjudicaUY.AppUY.dedyn.io/"
