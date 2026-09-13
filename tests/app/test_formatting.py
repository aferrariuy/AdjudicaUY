"""Unit tests for locale-aware formatting helpers."""

from __future__ import annotations

from decimal import Decimal

import pytest


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (Decimal("0"), "0,000 %"),
        (Decimal("0.0001"), "0,010 %"),
        (Decimal("0.0057"), "0,570 %"),
        (Decimal("0.009999"), "1,000 %"),
        (Decimal("0.01"), "1,0 %"),
        (Decimal("0.4231"), "42,3 %"),
        (Decimal("0.5"), "50,0 %"),
        (Decimal("1"), "100,0 %"),
    ],
)
def test_format_percent_adaptive_matrix(ratio: Decimal, expected: str) -> None:
    """Adaptive precision scales ratios to percentages across both branches.

    The ratio is always multiplied by 100: values at or above one percent
    use one decimal, smaller ones use three decimals so they stay visible.
    """

    from app.formatting import format_percent_adaptive

    assert format_percent_adaptive(ratio) == expected


def test_format_percent_adaptive_scales_sub_one_percent_shares() -> None:
    """A sub-1% ratio must still be scaled to a percentage (×100).

    Regression guard: the previous implementation returned the raw ratio for
    the sub-1% branch, rendering a 0.57% share as ``"0,006 %"`` instead of
    ``"0,570 %"``.
    """

    from app.formatting import format_percent_adaptive

    assert format_percent_adaptive(Decimal("0.0057")) == "0,570 %"
    assert format_percent_adaptive(Decimal("0.0001")) == "0,010 %"


@pytest.mark.parametrize(
    ("id_moneda", "expected"),
    [
        (None, "N/D"),
        (999, "N/D"),
        (0, "UYU"),
        (20, "USD"),
        (4, "UIX"),
    ],
)
def test_display_currency(id_moneda: int | None, expected: str) -> None:
    """Display codes preserve N/D fallback and known currency mappings."""

    from app.formatting import display_currency

    assert display_currency(id_moneda) == expected


def test_build_license_link_quotes_special_characters() -> None:
    """License identifiers remain safely URL-quoted in the detail path."""

    from app.formatting import build_license_link

    assert (
        build_license_link("LIC/ A?&")
        == "https://www.comprasestatales.gub.uy/consultas/detalle/id/LIC%2F%20A%3F%26"
    )


# ── Canonical entity paths ────────────────────────────────────────────────


def test_organism_path_quotes_spaces_and_separators() -> None:
    """An organism name is encoded into one safe path segment."""

    from app.formatting import organism_path

    assert organism_path("Ministerio de Interior") == (
        "/organism/Ministerio%20de%20Interior"
    )
    assert organism_path("A/B & C") == "/organism/A%2FB%20%26%20C"


def test_company_path_quotes_both_segments_independently() -> None:
    """Neither document half may swallow the separator between them."""

    from app.formatting import company_path

    assert company_path("RUT", "210000000012") == "/company/RUT/210000000012"
    assert company_path("RUT/X &", "00 1/2?") == (
        "/company/RUT%2FX%20%26/00%201%2F2%3F"
    )


def test_entity_paths_never_emit_xml_metacharacters() -> None:
    """Encoding is what lets the sitemap interpolate these without escaping.

    ``quote(..., safe="")`` percent-encodes ``&``, ``<`` and ``>``, so the
    resulting URLs cannot terminate the ``<loc>`` element early. This is the
    property the sitemap depends on; it is asserted rather than assumed.
    """

    from app.formatting import company_path, organism_path

    for path in (
        organism_path("<a> & </b>"),
        company_path("<x>", "&<>"),
    ):
        assert all(char not in path for char in "<>&")
