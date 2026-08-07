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
