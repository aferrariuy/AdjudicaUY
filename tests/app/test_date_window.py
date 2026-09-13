"""Tests for the effective date window and its 5-year cap.

``date_from``/``date_to`` are both optional in the filter form, and a
one-sided request used to be served with the missing side left *unbounded*:
``?date_from=1990-01-01`` produced ``>= 1990-01-01`` with no upper bound, and
``?date_to=2023-12-31`` produced ``<= 2023-12-31`` with no lower bound. The
5-year cap in ``validate_date_params`` only ran when BOTH bounds were present,
so it never applied to those requests.

Measured on production, an uncapped request costs 12-16s of aggregate work
against 2.25s for a one-year window, and because the aggregate cache key
includes the filters, every distinct date value is a fresh cache miss — a
cheap GET could force a full-dataset aggregate per request.

The contract asserted here:

* a one-sided window always has BOTH bounds materialized before it reaches
  the service layer, so no request can scan an unbounded amount of history;
* the derived bound is part of the filters, so the filter form displays the
  window that was actually queried instead of hiding it;
* the cap is enforced on the *effective* span, so a one-sided request that
  would exceed it is rejected instead of being computed;
* the natural in-cap queries ("desde hace un año", "hasta hace un año") keep
  working unchanged.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from app.services.filters import (
    MAX_DATE_RANGE_DAYS,
    DateValidationError,
    effective_date_window,
    filters_from_query_params,
    validate_date_params,
)

# A fixed "today" so the pure-helper tests never depend on the wall clock.
TODAY = date(2026, 9, 13)


def _iso(days_ago: int) -> str:
    """A date ``days_ago`` before the real today, for the HTTP-level tests.

    Those go through the routes, which read the real clock, so anchoring to the real
    today keeps the span exact whenever the suite runs. The unit tests below inject
    :data:`TODAY` instead — a fixed date would drift out of the cap eventually.
    """

    return (date.today() - timedelta(days=days_ago)).isoformat()


def _iso_before(days_ago: int) -> str:
    """A date ``days_ago`` before :data:`TODAY`, paired with ``today=TODAY``."""

    return (TODAY - timedelta(days=days_ago)).isoformat()


# ── The pure helper ────────────────────────────────────────────────────


def test_window_with_both_bounds_is_unchanged() -> None:
    dfrom, dto = date(2024, 1, 1), date(2024, 6, 30)
    assert effective_date_window(dfrom, dto, today=TODAY) == (dfrom, dto)


def test_window_with_no_bounds_is_unchanged() -> None:
    """No bound at all is not this function's business to fill."""

    assert effective_date_window(None, None, today=TODAY) == (None, None)


def test_missing_upper_bound_is_anchored_at_today() -> None:
    """'desde X' means 'X until now', so the span becomes measurable."""

    dfrom = date(2024, 1, 1)
    assert effective_date_window(dfrom, None, today=TODAY) == (dfrom, TODAY)


def test_future_lower_bound_collapses_instead_of_reversing() -> None:
    """A future 'desde' yields an empty window, not a reversed-range error."""

    future = date(2030, 1, 1)
    assert effective_date_window(future, None, today=TODAY) == (future, future)


def test_missing_lower_bound_is_anchored_at_the_cap() -> None:
    """'hasta X' keeps meaning 'before X', but never more than the cap."""

    dto = date(2023, 12, 31)
    dfrom, resolved_to = effective_date_window(None, dto, today=TODAY)
    assert dfrom is not None
    assert resolved_to is not None
    assert resolved_to == dto
    assert dfrom == dto - timedelta(days=MAX_DATE_RANGE_DAYS)
    assert (resolved_to - dfrom).days == MAX_DATE_RANGE_DAYS


# ── Validation ─────────────────────────────────────────────────────────


def test_single_sided_from_beyond_the_cap_is_rejected() -> None:
    """The gap this change closes: 1990 → today is not a range to compute."""

    with pytest.raises(DateValidationError, match="5 años"):
        validate_date_params(
            {"date_from": _iso_before(MAX_DATE_RANGE_DAYS + 1)}, today=TODAY
        )


def test_single_sided_from_within_the_cap_is_accepted() -> None:
    validate_date_params({"date_from": _iso_before(365)}, today=TODAY)


def test_single_sided_from_at_the_cap_boundary_is_accepted() -> None:
    validate_date_params({"date_from": _iso_before(MAX_DATE_RANGE_DAYS)}, today=TODAY)


def test_single_sided_to_is_always_within_the_cap() -> None:
    """A lone 'hasta' is anchored at the cap, so its span can never exceed it."""

    for days_ago in (0, 365, 3650, 36500):
        validate_date_params({"date_to": _iso_before(days_ago)}, today=TODAY)


def test_rejection_message_tells_the_user_what_to_change() -> None:
    """A one-sided rejection must not read like a two-sided one."""

    with pytest.raises(DateValidationError) as excinfo:
        validate_date_params({"date_from": "1990-01-01"}, today=TODAY)

    message = str(excinfo.value)
    assert "5 años" in message
    assert "Hasta" in message


def test_two_sided_rejection_keeps_the_original_message() -> None:
    with pytest.raises(DateValidationError, match="5 años") as excinfo:
        validate_date_params(
            {"date_from": "1990-01-01", "date_to": "2026-01-01"}, today=TODAY
        )

    assert "Hasta'" not in str(excinfo.value)


def test_unparseable_date_still_rejected() -> None:
    with pytest.raises(DateValidationError, match="Formato de fecha inválido"):
        validate_date_params({"date_from": "not-a-date"}, today=TODAY)


def test_reversed_range_still_rejected() -> None:
    with pytest.raises(DateValidationError, match="posterior"):
        validate_date_params(
            {"date_from": "2025-12-01", "date_to": "2025-01-01"}, today=TODAY
        )


# ── Materialization into the filters ───────────────────────────────────


def test_filters_materialize_the_missing_upper_bound() -> None:
    """The requested window is what reaches the query, so it is bounded."""

    filters = filters_from_query_params({"date_from": _iso_before(365)}, today=TODAY)

    assert filters.date_from == TODAY - timedelta(days=365)
    assert filters.date_to == TODAY


def test_filters_materialize_the_missing_lower_bound() -> None:
    """This is the regression guard for the 13s uncapped query."""

    filters = filters_from_query_params({"date_to": "2023-12-31"}, today=TODAY)

    assert filters.date_to == date(2023, 12, 31)
    assert filters.date_from == date(2023, 12, 31) - timedelta(days=MAX_DATE_RANGE_DAYS)


def test_filters_leave_a_two_sided_window_alone() -> None:
    filters = filters_from_query_params(
        {"date_from": "2024-01-01", "date_to": "2024-12-31"}, today=TODAY
    )

    assert (filters.date_from, filters.date_to) == (
        date(2024, 1, 1),
        date(2024, 12, 31),
    )


def test_every_served_window_is_bounded() -> None:
    """No input shape produces an open-ended window any more."""

    cases: list[dict[str, str | None]] = [
        {"date_from": _iso_before(365)},
        {"date_to": _iso_before(365)},
        {"date_from": _iso_before(365), "date_to": _iso_before(30)},
    ]
    for params in cases:
        filters = filters_from_query_params(params, today=TODAY)
        assert filters.date_from is not None, params
        assert filters.date_to is not None, params
        span = (filters.date_to - filters.date_from).days
        assert span <= MAX_DATE_RANGE_DAYS, params


# ── Endpoint level ─────────────────────────────────────────────────────


ENDPOINTS = [
    "/",
    "/adjudications",
    "/adjudications/export",
    "/organism/BPS",
    "/organism/BPS/partial",
    "/company/RUT/1",
    "/company/RUT/1/partial",
    "/company/RUT/1/export",
]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_over_cap_single_sided_request_is_rejected(client: Any, path: str) -> None:
    """Every route inherits the cap, not just the dashboard."""

    response = client.get(f"{path}?date_from={_iso(MAX_DATE_RANGE_DAYS + 1)}")
    assert response.status_code == 422
    assert "5 años" in response.text


@pytest.mark.parametrize(
    "path",
    ["/", "/adjudications", "/organism/BPS", "/organism/BPS/partial"],
)
def test_in_cap_single_sided_request_still_works(client: Any, path: str) -> None:
    response = client.get(f"{path}?date_from={_iso(365)}")
    assert response.status_code == 200


def test_derived_bound_is_visible_in_the_filter_form(
    client: Any, make_adjudication: Any
) -> None:
    """The window that was queried is shown, never applied silently.

    The form renders from the filters object, so materializing the missing
    bound there is what keeps "lo que ves es lo que se consultó" true.
    """

    make_adjudication(organism="Organismo Visible")

    response = client.get("/organism/Organismo%20Visible?date_from=2024-01-01")

    assert response.status_code == 200
    assert f'value="{date.today().isoformat()}"' in response.text
    assert 'value="2024-01-01"' in response.text


def test_missing_lower_bound_is_visible_in_the_filter_form(client: Any) -> None:
    response = client.get("/?date_to=2023-12-31")

    assert response.status_code == 200
    expected_from = date(2023, 12, 31) - timedelta(days=MAX_DATE_RANGE_DAYS)
    assert f'value="{expected_from.isoformat()}"' in response.text


# ── The injected clock ─────────────────────────────────────────────────


def test_validation_measures_the_cap_against_the_injected_day() -> None:
    """The same window flips verdicts when only ``today`` changes.

    This is what makes the parameter a real seam rather than a decoration: the cap is
    measured against the day it is handed, so a caller that pins it gets a verdict that
    cannot move with the wall clock.
    """

    # 1826 days before TODAY is over the cap. The same date fits inside it when the
    # clock it is measured against moves 30 days *earlier* — the span shrinks as the
    # anchor recedes, so the direction matters and is the point of the test.
    params: dict[str, str | None] = {"date_from": _iso_before(MAX_DATE_RANGE_DAYS + 1)}

    with pytest.raises(DateValidationError, match="5 años"):
        validate_date_params(params, today=TODAY)

    validate_date_params(params, today=TODAY - timedelta(days=30))


def test_materialization_anchors_on_the_injected_day() -> None:
    """The window is anchored on the day the caller pinned, not on the real one.

    The pinned day is deliberately one the wall clock will never read, which is what
    makes the assertion able to fail. An earlier version of this test pinned
    :data:`TODAY` — the date the suite happened to run on — so an implementation that
    ignored the argument and read ``date.today()`` produced the same value and stayed
    green.
    """

    pinned = date(2019, 1, 1)
    assert pinned != date.today(), "pick a day the wall clock cannot be reading"

    filters = filters_from_query_params({"date_from": "2018-01-01"}, today=pinned)

    assert filters.date_to == pinned


def test_omitting_the_clock_reads_the_real_one() -> None:
    """The parameter is optional, and its absence means "read the clock now"."""

    filters = filters_from_query_params({"date_from": "2024-01-01"})

    assert filters.date_to == date.today()
