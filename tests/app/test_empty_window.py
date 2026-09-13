"""Tests for the informative empty state on entity pages.

An entity page whose active date window has no adjudications used to render
each widget's own empty branch: the same generic sentence four times, never
naming the window and never offering a way out. On production, 39 of 314
organism pages (12,4%) were in that state with real historical activity
behind them — 4.75 billion UYU not visible by default.

The contract asserted here:

* the empty state names the window that was actually queried;
* it reports the entity's last year with activity and its real figure, so the
  page carries content of its own instead of four copies of one sentence;
* it offers a link to that year, and following that link reaches real data;
* a page with data in the window renders the widgets and no empty state;
* nothing extra is looked up when the window is not empty.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

TODAY = date.today()
CURRENT_YEAR = TODAY.year
OLD_YEAR = CURRENT_YEAR - 7
OLD_DATE = date(OLD_YEAR, 5, 1)

ORGANISM = "Organismo Viejo"
ORGANISM_PATH = "/organism/Organismo%20Viejo"
COMPANY_TYPE = "RUT"
COMPANY_NUMBER = "77000001"
COMPANY_PATH = f"/company/{COMPANY_TYPE}/{COMPANY_NUMBER}"

EMPTY_HEADING = "Sin adjudicaciones en el período"


# ── The aggregate ──────────────────────────────────────────────────────


def test_latest_activity_ignores_the_date_window(
    db_session: Any, make_adjudication: Any
) -> None:
    """Asked with a 2026 window, it still reports the 2019 activity."""

    from app.services.dashboard import latest_activity_date
    from app.services.filters import AdjudicationFilters

    make_adjudication(organism=ORGANISM, date=OLD_DATE)

    filters = AdjudicationFilters(
        organism_exact=ORGANISM,
        date_from=date(CURRENT_YEAR, 1, 1),
        date_to=date(CURRENT_YEAR, 12, 31),
    )

    assert latest_activity_date(db_session, filters) == OLD_DATE


def test_latest_activity_respects_entity_scope(
    db_session: Any, make_adjudication: Any
) -> None:
    from app.services.dashboard import latest_activity_date
    from app.services.filters import AdjudicationFilters

    make_adjudication(organism=ORGANISM, date=OLD_DATE)
    make_adjudication(organism="Otro Organismo", date=date(2024, 3, 1))

    assert (
        latest_activity_date(db_session, AdjudicationFilters(organism_exact=ORGANISM))
        == OLD_DATE
    )
    assert latest_activity_date(
        db_session, AdjudicationFilters(organism_exact="Otro Organismo")
    ) == date(2024, 3, 1)


def test_latest_activity_is_none_without_matching_rows(db_session: Any) -> None:
    from app.services.dashboard import latest_activity_date
    from app.services.filters import AdjudicationFilters

    assert (
        latest_activity_date(
            db_session, AdjudicationFilters(organism_exact="No Existe")
        )
        is None
    )


def test_latest_activity_ignores_awards_less_compras(db_session: Any) -> None:
    """A compra with no adjudicacion row must not name a year.

    Such a year would render empty for the same reason the current window
    does, so pointing the visitor at it would be a dead end.
    """

    from app.models.compra import Compra
    from app.services.dashboard import latest_activity_date
    from app.services.filters import AdjudicationFilters

    db_session.add(
        Compra(
            id_compra="parent-only",
            fecha_pub_adj=OLD_DATE,
            id_tipocompra="CD",
            id_inciso=4,
            id_ue=1,
            organismo=ORGANISM,
            source_url="https://example.test/xml",
        )
    )
    db_session.commit()

    assert (
        latest_activity_date(db_session, AdjudicationFilters(organism_exact=ORGANISM))
        is None
    )


# ── The view-model ─────────────────────────────────────────────────────


def test_empty_window_view_formats_the_window_and_the_link() -> None:
    from app.presenters import _build_empty_window_view

    view = _build_empty_window_view(
        window_from=date(2026, 1, 1),
        window_to=date(2026, 12, 31),
        last_activity=OLD_DATE,
        last_year_total_amount=Decimal("1000.00"),
        last_year_purchase_count=1,
        path=ORGANISM_PATH,
    )

    assert view.window_label == "01/01/2026 – 31/12/2026"
    assert view.last_year == OLD_YEAR
    assert view.last_year_url == (
        f"{ORGANISM_PATH}?date_from={OLD_YEAR}-01-01&date_to={OLD_YEAR}-12-31"
    )
    assert view.last_year_total_amount == Decimal("1000.00")
    assert view.last_year_purchase_count == 1


def test_empty_window_view_without_any_history() -> None:
    from app.presenters import _build_empty_window_view

    view = _build_empty_window_view(
        window_from=date(2026, 1, 1),
        window_to=date(2026, 12, 31),
        last_activity=None,
        last_year_total_amount=None,
        last_year_purchase_count=None,
        path=ORGANISM_PATH,
    )

    assert view.last_year is None
    assert view.last_year_url is None


# ── Organism page ──────────────────────────────────────────────────────


@pytest.fixture
def old_organism(make_adjudication: Any) -> None:
    """An organism whose only award is old, so the default window is empty."""

    make_adjudication(
        organism=ORGANISM,
        date=OLD_DATE,
        winning_company="Empresa Histórica",
        amount_uyu=Decimal("1000.00"),
    )


@pytest.mark.usefixtures("old_organism")
def test_organism_empty_state_names_the_window_and_the_last_year(
    client: Any,
) -> None:
    from app.formatting import format_count, format_uyu

    response = client.get(ORGANISM_PATH)

    assert response.status_code == 200
    body = response.text
    assert EMPTY_HEADING in body
    # The window that was actually queried, not a generic "no data".
    assert f"01/01/{CURRENT_YEAR} – 31/12/{CURRENT_YEAR}" in body
    # The real figure from the last year with activity.
    assert str(OLD_YEAR) in body
    assert format_uyu(Decimal("1000.00")) in body
    assert format_count(1) in body
    # ...and a way out.
    assert (
        f'href="{ORGANISM_PATH}?date_from={OLD_YEAR}-01-01'
        f'&amp;date_to={OLD_YEAR}-12-31"' in body
    )


@pytest.mark.usefixtures("old_organism")
def test_organism_empty_state_replaces_the_repeated_widget_copy(
    client: Any,
) -> None:
    body = client.get(ORGANISM_PATH).text

    assert body.count(EMPTY_HEADING) == 1
    # The per-widget sentences are gone: four copies of one sentence were the
    # problem, not the solution.
    assert "Sin datos disponibles" not in body
    assert "No hay datos para los filtros aplicados" not in body


@pytest.mark.usefixtures("old_organism")
def test_organism_partial_also_renders_the_empty_state(client: Any) -> None:
    """The HTMX path must not degrade to the old repeated copy."""

    response = client.get(f"{ORGANISM_PATH}/partial")

    assert response.status_code == 200
    assert EMPTY_HEADING in response.text
    assert f"{OLD_YEAR}-01-01" in response.text


@pytest.mark.usefixtures("old_organism")
def test_the_offered_link_reaches_real_data(client: Any) -> None:
    """The way out is real, not decorative: it lands on the populated year."""

    response = client.get(
        f"{ORGANISM_PATH}?date_from={OLD_YEAR}-01-01&date_to={OLD_YEAR}-12-31"
    )

    assert response.status_code == 200
    assert EMPTY_HEADING not in response.text
    assert "Empresa Histórica" in response.text


def test_organism_with_data_in_the_window_has_no_empty_state(
    client: Any, make_adjudication: Any
) -> None:
    make_adjudication(organism="Organismo Activo", date=date(CURRENT_YEAR, 3, 1))

    response = client.get("/organism/Organismo%20Activo")

    assert response.status_code == 200
    assert EMPTY_HEADING not in response.text


def test_healthy_window_runs_no_empty_state_lookup(
    client: Any, make_adjudication: Any
) -> None:
    """The empty-state lookups must be skipped when the window has data."""

    from unittest.mock import patch

    make_adjudication(organism="Organismo Activo", date=date(CURRENT_YEAR, 3, 1))

    with patch(
        "app.routes.common.latest_activity_date",
        side_effect=AssertionError("must not probe the history for a full window"),
    ):
        response = client.get("/organism/Organismo%20Activo")

    assert response.status_code == 200


# ── Company page ───────────────────────────────────────────────────────


@pytest.fixture
def old_company(make_adjudication: Any) -> None:
    make_adjudication(
        winning_company="Empresa Histórica",
        company_document_type=COMPANY_TYPE,
        company_document=COMPANY_NUMBER,
        date=OLD_DATE,
        amount_uyu=Decimal("2500.00"),
    )


@pytest.mark.usefixtures("old_company")
def test_company_empty_state_names_the_window_and_the_last_year(
    client: Any,
) -> None:
    response = client.get(COMPANY_PATH)

    assert response.status_code == 200
    body = response.text
    assert EMPTY_HEADING in body
    assert f"01/01/{CURRENT_YEAR} – 31/12/{CURRENT_YEAR}" in body
    assert f"{COMPANY_PATH}?date_from={OLD_YEAR}-01-01" in body.replace("&amp;", "&")


@pytest.mark.usefixtures("old_company")
def test_company_partial_also_renders_the_empty_state(client: Any) -> None:
    response = client.get(f"{COMPANY_PATH}/partial")

    assert response.status_code == 200
    assert EMPTY_HEADING in response.text


def test_company_with_data_in_the_window_has_no_empty_state(
    client: Any, make_adjudication: Any
) -> None:
    make_adjudication(
        winning_company="Empresa Activa",
        company_document_type=COMPANY_TYPE,
        company_document="77000002",
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get(f"/company/{COMPANY_TYPE}/77000002")

    assert response.status_code == 200
    assert EMPTY_HEADING not in response.text
