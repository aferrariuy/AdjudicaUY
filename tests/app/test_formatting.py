"""Unit tests for locale-aware formatting helpers."""

from __future__ import annotations

from decimal import Decimal

import pytest


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (Decimal("0.5"), "50,0 %"),
        (Decimal("0.0057"), "0,006 %"),
        (Decimal("0.0001"), "0,000 %"),
        (Decimal("0.4231"), "42,3 %"),
    ],
)
def test_format_percent_adaptive_matrix(ratio: Decimal, expected: str) -> None:
    """Adaptive precision keeps small KPI shares visible in es-UY format."""

    from app.formatting import format_percent_adaptive

    assert format_percent_adaptive(ratio) == expected


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
