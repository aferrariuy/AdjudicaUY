"""Unit tests for pure chart presenters."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.presenters import _build_trend_chart_payload


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
