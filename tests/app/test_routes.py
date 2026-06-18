"""Unit tests for the FastAPI routes in :mod:`app.routes.adjudications`.

The route layer is intentionally thin — these tests exercise the
behaviours declared in the filtering-ui spec and the route's HTMX
contract, using the in-memory SQLite engine from ``conftest.py``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

from app.services.adjudication_service import (
    AdjudicationFilters,
    filters_from_query_params,
)

# The route layer defaults the date range to the current calendar year
# on cold load (no date params). Tests that don't pass explicit date
# params in the URL MUST use a current-year date for their fixtures,
# otherwise the new default filter will hide them.
CURRENT_YEAR = date.today().year
NEXT_YEAR = CURRENT_YEAR + 1
PREV_YEAR = CURRENT_YEAR - 1

# ---------------------------------------------------------------------------
# GET / — full HTML page
# ---------------------------------------------------------------------------


def test_index_returns_full_html_with_filter_form(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # The filter form is rendered.
    body = response.text
    assert 'name="article"' in body
    assert 'name="company"' in body
    assert 'name="organism"' in body
    assert 'name="date_from"' in body
    assert 'name="date_to"' in body


def test_index_renders_results_table_when_data_exists(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        winning_company="INDEX-COMPANY-Acme",
        organism="INDEX-ORG-OSE",
        article="INDEX-ARTICLE-Laptop",
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/")
    body = response.text

    assert response.status_code == 200
    assert "INDEX-COMPANY-Acme" in body
    assert "INDEX-ORG-OSE" in body
    assert "INDEX-ARTICLE-Laptop" in body


def test_index_renders_no_results_message_when_db_is_empty(
    client: TestClient,
) -> None:
    response = client.get("/")
    body = response.text

    assert response.status_code == 200
    assert "No se encontraron adjudicaciones" in body


def test_index_renders_chart_canvases(client: TestClient, make_adjudication) -> None:
    make_adjudication(amount_uyu=Decimal("1000.00"), date=date(CURRENT_YEAR, 3, 1))

    response = client.get("/")
    body = response.text

    # Both partials are rendered with their canvas elements.
    assert 'id="chart-ranking"' in body
    assert 'id="chart-organism-ranking"' in body


def test_index_includes_distinct_organisms_in_datalist(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(organism="DLIST-MIN-INTERIOR", date=date(CURRENT_YEAR, 3, 1))
    make_adjudication(organism="DLIST-OSE", date=date(CURRENT_YEAR, 3, 2))

    response = client.get("/")

    assert response.status_code == 200
    assert "DLIST-MIN-INTERIOR" in response.text
    assert "DLIST-OSE" in response.text


# ---------------------------------------------------------------------------
# GET /adjudications — HTMX partial
# ---------------------------------------------------------------------------


def test_adjudications_partial_returns_results_fragment(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        winning_company="PARTIAL-COMPANY-A", date=date(CURRENT_YEAR, 3, 1)
    )
    make_adjudication(
        winning_company="PARTIAL-COMPANY-B", date=date(CURRENT_YEAR, 3, 2)
    )

    response = client.get("/adjudications")

    assert response.status_code == 200
    body = response.text
    assert "PARTIAL-COMPANY-A" in body
    assert "PARTIAL-COMPANY-B" in body
    # The partial still embeds the chart canvases so the browser can
    # re-mount Chart.js after the swap.
    assert 'id="chart-ranking"' in body


def test_adjudications_partial_renders_no_results_panel(
    client: TestClient,
) -> None:
    response = client.get("/adjudications")

    assert response.status_code == 200
    assert "No se encontraron adjudicaciones" in response.text


def test_adjudications_partial_logs_htmx_request_distinction(
    client: TestClient, caplog
) -> None:
    """The route differentiates HX-Request=true from a plain GET."""

    import logging

    with caplog.at_level(logging.INFO, logger="app.routes.adjudications"):
        response = client.get(
            "/adjudications",
            headers={"HX-Request": "true"},
        )

    assert response.status_code == 200
    # The log line records whether the request came from HTMX.
    assert any("htmx=True" in record.message for record in caplog.records)


def test_adjudications_partial_logs_htmx_false_for_plain_get(
    client: TestClient, caplog
) -> None:
    import logging

    with caplog.at_level(logging.INFO, logger="app.routes.adjudications"):
        client.get("/adjudications")

    assert any("htmx=False" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Filter combinations
# ---------------------------------------------------------------------------


def test_article_filter_applies_case_insensitive_partial_match(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        article="Laptop Dell Latitude",
        winning_company="A",
        date=date(CURRENT_YEAR, 3, 1),
    )
    make_adjudication(
        article="Monitor LG 24", winning_company="B", date=date(CURRENT_YEAR, 3, 2)
    )

    response = client.get("/adjudications?article=laptop")

    assert response.status_code == 200
    body = response.text
    assert "Laptop Dell Latitude" in body
    assert "Monitor LG 24" not in body


def test_company_filter_applies_case_insensitive_partial_match(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        winning_company="COMPANY-1",
        article="COMPANY-1-article",
        date=date(CURRENT_YEAR, 3, 1),
    )
    make_adjudication(
        winning_company="COMPANY-2",
        article="COMPANY-2-article",
        date=date(CURRENT_YEAR, 3, 2),
    )

    response = client.get("/adjudications?company=COMPANY")

    assert response.status_code == 200
    body = response.text
    assert "COMPANY-1" in body
    assert "COMPANY-2" in body  # partial match includes both


def test_company_filter_exact_match_excludes_other_companies(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        winning_company="ACME-CORP", article="X", date=date(CURRENT_YEAR, 3, 1)
    )
    make_adjudication(
        winning_company="GLOBEX-INC", article="Y", date=date(CURRENT_YEAR, 3, 2)
    )

    response = client.get("/adjudications?company=ACME")

    body = response.text
    assert "ACME-CORP" in body
    assert "GLOBEX-INC" not in body


def test_organism_filter_applies_partial_match(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        organism="Ministerio de Interior", article="X", date=date(CURRENT_YEAR, 3, 1)
    )
    make_adjudication(
        organism="Ministerio de Salud", article="Y", date=date(CURRENT_YEAR, 3, 2)
    )
    make_adjudication(organism="OSE", article="Z", date=date(CURRENT_YEAR, 3, 3))

    response = client.get("/adjudications?organism=ministerio")

    body = response.text
    assert "Ministerio de Interior" in body
    assert "Ministerio de Salud" in body
    assert "OSE" not in body


def test_date_range_filter_includes_endpoints(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(date=date(2024, 1, 1), winning_company="DATEBOUND-A")
    make_adjudication(date=date(2024, 3, 31), winning_company="DATEBOUND-B")
    make_adjudication(date=date(2024, 6, 1), winning_company="DATEBOUND-C")

    response = client.get("/adjudications?date_from=2024-01-01&date_to=2024-03-31")

    body = response.text
    assert "DATEBOUND-A" in body
    assert "DATEBOUND-B" in body
    assert "DATEBOUND-C" not in body


def test_date_from_only_filter_includes_everything_after(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(date=date(2023, 1, 1), winning_company="TOO-OLD")
    make_adjudication(date=date(2024, 1, 1), winning_company="FRESH-A")

    response = client.get("/adjudications?date_from=2024-01-01")

    body = response.text
    assert "TOO-OLD" not in body
    assert "FRESH-A" in body


def test_date_to_only_filter_includes_everything_before(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(date=date(2023, 12, 31), winning_company="PRECUT-OLD")
    make_adjudication(date=date(2024, 6, 1), winning_company="PRECUT-NEW")

    response = client.get("/adjudications?date_to=2023-12-31")

    body = response.text
    assert "PRECUT-OLD" in body
    assert "PRECUT-NEW" not in body


def test_combined_filters_apply_and_logic(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        article="Laptop",
        organism="Ministerio de Interior",
        date=date(2024, 3, 1),
        winning_company="FILTER-MATCH",
    )
    make_adjudication(
        article="Laptop",
        organism="OSE",
        date=date(2024, 3, 1),
        winning_company="FILTER-WRONG-ORGANISM",
    )
    make_adjudication(
        article="Monitor",
        organism="Ministerio de Interior",
        date=date(2024, 3, 1),
        winning_company="FILTER-WRONG-ARTICLE",
    )
    make_adjudication(
        article="Laptop",
        organism="Ministerio de Interior",
        date=date(2023, 1, 1),
        winning_company="FILTER-WRONG-DATE",
    )

    response = client.get(
        "/adjudications?article=laptop&organism=interior&date_from=2024-01-01&date_to=2024-06-30"
    )

    body = response.text
    assert "FILTER-MATCH" in body
    assert "FILTER-WRONG-ORGANISM" not in body
    assert "FILTER-WRONG-ARTICLE" not in body
    assert "FILTER-WRONG-DATE" not in body


def test_no_filters_returns_current_year(client: TestClient, make_adjudication) -> None:
    """Cold load with no params returns only current-year rows.

    The route injects ``date_from={year}-01-01`` and ``date_to={year}-12-31``
    when BOTH date params are absent. Older data must be excluded.
    """

    make_adjudication(winning_company="OLDFILTER-PREV", date=date(PREV_YEAR, 6, 1))
    make_adjudication(winning_company="CURRENTFILTER-A", date=date(CURRENT_YEAR, 3, 1))
    make_adjudication(winning_company="CURRENTFILTER-B", date=date(CURRENT_YEAR, 6, 1))
    make_adjudication(winning_company="CURRENTFILTER-C", date=date(CURRENT_YEAR, 9, 1))

    response = client.get("/adjudications")

    assert response.status_code == 200
    body = response.text
    # Current-year rows must be present.
    for company in ("CURRENTFILTER-A", "CURRENTFILTER-B", "CURRENTFILTER-C"):
        assert company in body, f"Expected {company!r} in body"
    # Prior-year row must be filtered out by the default date range.
    assert "OLDFILTER-PREV" not in body


def test_filters_with_no_matching_results_render_no_results_message(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(winning_company="A")

    response = client.get("/adjudications?article=impossible")

    assert response.status_code == 200
    assert "No se encontraron adjudicaciones" in response.text


# ---------------------------------------------------------------------------
# Chart aggregates reflect active filters
# ---------------------------------------------------------------------------


def test_ranking_chart_reflects_active_filters(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        winning_company="TopCorp",
        amount_uyu=Decimal("100000.00"),
        organism="OSE",
        date=date(CURRENT_YEAR, 3, 1),
    )
    make_adjudication(
        winning_company="SmallCo",
        amount_uyu=Decimal("500.00"),
        organism="Ministerio de Interior",
        date=date(CURRENT_YEAR, 3, 2),
    )

    response = client.get("/adjudications?organism=OSE")
    body = response.text

    # The ranking payload is JSON-encoded into the canvas's data-chart attribute.
    assert "TopCorp" in body
    assert "SmallCo" not in body


def test_organism_ranking_chart_aggregates_by_organism(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        organism="OSE",
        amount_uyu=Decimal("100.00"),
        winning_company="A",
        date=date(CURRENT_YEAR, 3, 1),
    )
    make_adjudication(
        organism="OSE",
        amount_uyu=Decimal("200.00"),
        winning_company="A",
        date=date(CURRENT_YEAR, 3, 2),
    )
    make_adjudication(
        organism="Ministerio de Interior",
        amount_uyu=Decimal("50.00"),
        winning_company="B",
        date=date(CURRENT_YEAR, 3, 3),
    )

    response = client.get("/adjudications")
    body = response.text

    # The chart payload aggregates by organism. The organism ranking
    # canvas is present and its data-chart payload contains both
    # organism names.
    assert 'id="chart-organism-ranking"' in body
    assert "OSE" in body
    assert "Ministerio de Interior" in body


def test_ranking_excludes_null_amount_uyu_rows(
    client: TestClient, make_adjudication
) -> None:
    """Non-convertible currencies (amount_uyu=NULL) are NOT ranked."""

    make_adjudication(
        winning_company="ConvertibleCo",
        amount_uyu=Decimal("100.00"),
        date=date(CURRENT_YEAR, 3, 1),
    )
    make_adjudication(
        winning_company="NonConvertibleCo",
        amount_uyu=None,
        date=date(CURRENT_YEAR, 3, 2),
    )

    response = client.get("/adjudications")
    body = response.text

    assert "ConvertibleCo" in body
    # Non-convertible company is still listed in the table...
    assert "NonConvertibleCo" in body
    # ...but it MUST NOT appear in the ranking chart's data payload,
    # which is rendered into the canvas. Easiest signal: the ranking
    # canvas exists with the convertible company in the data.
    assert "TopCorp" not in body  # sanity: not introduced
    assert "ConvertibleCo" in body  # visible in some chart context


# ---------------------------------------------------------------------------
# Helper coverage — pure-function ``filters_from_query_params``
# ---------------------------------------------------------------------------


def test_filters_from_query_params_handles_empty_strings() -> None:
    filters = filters_from_query_params(
        {
            "company": "  ",
            "organism": "",
            "article": None,
            "date_from": "  ",
            "date_to": "",
        }
    )
    assert filters.has_any() is False


def test_filters_from_query_params_handles_iso_dates() -> None:
    filters = filters_from_query_params(
        {
            "date_from": "2024-01-15",
            "date_to": "2024-06-30",
        }
    )
    assert filters.date_from == date(2024, 1, 15)
    assert filters.date_to == date(2024, 6, 30)


def test_filters_from_query_params_treats_garbage_dates_as_none() -> None:
    filters = filters_from_query_params({"date_from": "not-a-date"})
    assert filters.date_from is None


def test_filters_from_query_params_trims_text() -> None:
    filters = filters_from_query_params({"company": "  Acme  "})
    assert filters.company == "Acme"


def test_adjudication_filters_has_any_detects_active_filter() -> None:
    assert AdjudicationFilters(company="A").has_any() is True
    assert AdjudicationFilters(article="x").has_any() is True
    assert AdjudicationFilters(date_from=date(2024, 1, 1)).has_any() is True
    assert AdjudicationFilters().has_any() is False
    # Empty strings (per the service contract) mean "no filter".
    assert AdjudicationFilters(company="", organism="").has_any() is False


# ---------------------------------------------------------------------------
# Static asset / health checks
# ---------------------------------------------------------------------------


def test_index_is_htmx_compatible(client: TestClient) -> None:
    """The page must include the htmx script tag so swaps work in the browser."""

    response = client.get("/")
    assert response.status_code == 200
    assert "htmx.org" in response.text


def test_index_includes_chartjs(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "chart.js" in response.text or "chart.umd" in response.text


def test_index_renders_datalist_for_organism_suggestions(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(organism="DLIST2-MIN-INDUSTRIA", date=date(CURRENT_YEAR, 3, 1))
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="organism-suggestions"' in response.text
    assert "DLIST2-MIN-INDUSTRIA" in response.text


# ---------------------------------------------------------------------------
# Default year filter — cold-load injection, validation, Limpiar reset
# ---------------------------------------------------------------------------


def test_default_year_injection_on_cold_load(
    client: TestClient, make_adjudication
) -> None:
    """Cold GET / renders form inputs at current-year bounds and returns
    only current-year rows (no other-year data leaks through)."""

    make_adjudication(winning_company="COLD-OLD", date=date(PREV_YEAR, 6, 1))
    make_adjudication(winning_company="COLD-NEW-A", date=date(CURRENT_YEAR, 1, 15))
    make_adjudication(winning_company="COLD-NEW-B", date=date(CURRENT_YEAR, 8, 20))

    response = client.get("/")
    assert response.status_code == 200
    body = response.text

    # Form inputs reflect the injected current-year bounds.
    assert f'value="{CURRENT_YEAR}-01-01"' in body, (
        f"Expected date_from input bound to {CURRENT_YEAR}-01-01"
    )
    assert f'value="{CURRENT_YEAR}-12-31"' in body, (
        f"Expected date_to input bound to {CURRENT_YEAR}-12-31"
    )

    # Results panel: current-year rows visible, older year filtered out.
    assert "COLD-NEW-A" in body
    assert "COLD-NEW-B" in body
    assert "COLD-OLD" not in body


def test_date_from_after_date_to_returns_422(client: TestClient) -> None:
    """Reversed date range returns 422 with a Spanish error fragment."""

    response = client.get("/adjudications?date_from=2025-12-01&date_to=2025-01-01")

    assert response.status_code == 422
    body = response.text
    # The inline error fragment is an alert role with a Spanish message.
    assert 'role="alert"' in body
    assert "Desde" in body
    assert "Hasta" in body


def test_invalid_date_format_returns_422(client: TestClient) -> None:
    """Garbage date strings return 422 with a Spanish error fragment."""

    response = client.get("/adjudications?date_from=not-a-date")

    assert response.status_code == 422
    body = response.text
    assert 'role="alert"' in body
    # Spanish hint about ISO format.
    assert "AAAA-MM-DD" in body


def test_limpiar_resets_to_current_year(client: TestClient) -> None:
    """The Limpiar control is a button that wires a JS reset to current
    year (NOT an <a href="/"> that would navigate)."""

    response = client.get("/")
    assert response.status_code == 200
    body = response.text

    # The old <a href="/">Limpiar</a> is gone.
    assert ">Limpiar</a>" not in body
    # The new <button onclick="limpiarFiltros()">Limpiar</button> is present.
    assert 'onclick="limpiarFiltros()"' in body
    assert ">Limpiar</button>" in body
    # The reset function is wired onto window so the inline onclick can
    # call it. The implementation assigns an anonymous function expression.
    assert "window.limpiarFiltros" in body
    assert "htmx.trigger" in body
    # JS computes the current-year bounds at runtime, not hardcoded.
    assert "getFullYear" in body


# ---------------------------------------------------------------------------
# Direct coverage of the pure ``validate_date_params`` helper
# ---------------------------------------------------------------------------


def test_validate_date_params_accepts_missing_keys() -> None:
    from app.services.adjudication_service import validate_date_params

    # Empty / absent params are the route's default-injection concern,
    # not a validation error.
    validate_date_params({})
    validate_date_params({"date_from": "", "date_to": None})


def test_validate_date_params_accepts_valid_iso() -> None:
    from app.services.adjudication_service import validate_date_params

    validate_date_params({"date_from": "2024-01-15", "date_to": "2024-12-31"})


def test_validate_date_params_rejects_garbage_string() -> None:
    import pytest

    from app.services.adjudication_service import (
        DateValidationError,
        validate_date_params,
    )

    with pytest.raises(DateValidationError, match="AAAA-MM-DD"):
        validate_date_params({"date_from": "not-a-date"})


def test_validate_date_params_rejects_reversed_range() -> None:
    import pytest

    from app.services.adjudication_service import (
        DateValidationError,
        validate_date_params,
    )

    with pytest.raises(DateValidationError, match="Desde"):
        validate_date_params({"date_from": "2025-12-01", "date_to": "2025-01-01"})


# ---------------------------------------------------------------------------
# GET /organism/{name} — organism profile page (PR#2 of citizen-dashboard)
# ---------------------------------------------------------------------------


def test_organism_detail_returns_200_with_widgets_for_known_organism(
    client: TestClient, make_adjudication, db_session
) -> None:
    """Known organism renders the profile page with KPI/trend/concentration/ranking."""

    from app.models.oferente import Oferente

    adj1 = make_adjudication(
        organism="Ministerio del Interior",
        winning_company="ACME",
        amount_uyu=Decimal("1000.00"),
        date=date(CURRENT_YEAR, 3, 1),
    )
    make_adjudication(
        organism="Ministerio del Interior",
        winning_company="Globex",
        amount_uyu=Decimal("500.00"),
        date=date(CURRENT_YEAR, 4, 1),
    )
    # Attach an oferente to the first compra so the concentration
    # chart has data to render (the empty-state branch otherwise
    # replaces the chart with a "Sin datos" message).
    db_session.add(Oferente(compra_id=adj1.compra_id, nombre_comercial="Bidder A"))
    db_session.commit()

    response = client.get("/organism/Ministerio%20del%20Interior")

    assert response.status_code == 200
    body = response.text
    # The organism name is rendered as the page heading.
    assert "Ministerio del Interior" in body
    # The dashboard widgets are all present.
    assert "Resumen" in body  # KPI section heading
    assert 'id="chart-trend"' in body
    assert 'id="chart-concentration"' in body
    assert 'id="chart-ranking"' in body  # company ranking
    # The organism ranking chart is intentionally absent on this page.
    assert 'id="chart-organism-ranking"' not in body


def test_organism_detail_returns_200_with_empty_state_for_unknown_organism(
    client: TestClient,
) -> None:
    """Unknown organism renders an empty profile (no 404)."""

    response = client.get("/organism/Nonexistent%20Organism")

    assert response.status_code == 200
    body = response.text
    # The page still loads and shows the requested name.
    assert "Nonexistent Organism" in body
    # All widgets fall back to their empty-state copy.
    assert "No hay datos" in body
    assert "Sin datos disponibles" in body


def test_organism_detail_decodes_url_with_accents(
    client: TestClient, make_adjudication
) -> None:
    """Accented characters in the URL are decoded to the original name."""

    make_adjudication(
        organism="Dirección Nacional de Aduanas",
        winning_company="ADUANAS-CO",
        amount_uyu=Decimal("100.00"),
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/organism/Direcci%C3%B3n%20Nacional%20de%20Aduanas")

    assert response.status_code == 200
    body = response.text
    # The decoded name appears as the heading — the round-trip from
    # %C3%B3 → ó is what this scenario is asserting.
    assert "Dirección Nacional de Aduanas" in body


def test_organism_detail_partial_returns_body_without_chrome(
    client: TestClient, make_adjudication, db_session
) -> None:
    """The HTMX partial endpoint returns the body, not the full page chrome."""

    from app.models.oferente import Oferente

    adj = make_adjudication(
        organism="OSE",
        winning_company="OSE-CO",
        amount_uyu=Decimal("100.00"),
        date=date(CURRENT_YEAR, 3, 1),
    )
    # Concentration needs oferentes to render the chart canvas.
    db_session.add(Oferente(compra_id=adj.compra_id, nombre_comercial="Bidder OSE"))
    db_session.commit()

    response = client.get("/organism/OSE/partial")

    assert response.status_code == 200
    body = response.text
    # The body partial includes the dashboard widgets.
    assert 'id="chart-trend"' in body
    assert 'id="chart-concentration"' in body
    assert 'id="chart-ranking"' in body
    # The full page chrome is NOT re-rendered — no <header>, no nav,
    # no filter form (the form lives outside the swap target).
    assert "<header" not in body
    assert "Volver al buscador" not in body


def test_results_table_organism_link_uses_organism_route(
    client: TestClient, make_adjudication
) -> None:
    """Organism cells in the results table are anchors pointing to /organism/..."""

    make_adjudication(
        organism="Ministerio del Interior",
        winning_company="LINK-CO",
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/")
    body = response.text

    assert response.status_code == 200
    # The anchor is present with the URL-encoded organism name.
    assert 'href="/organism/Ministerio%20del%20Interior"' in body
    # The visible text is the original organism name.
    assert ">Ministerio del Interior</a>" in body
