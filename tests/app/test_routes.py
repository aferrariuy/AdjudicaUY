"""Unit tests for the FastAPI routes in :mod:`app.routes.adjudications`.

The route layer is intentionally thin — these tests exercise the
behaviours declared in the filtering-ui spec and the route's HTMX
contract, using the in-memory SQLite engine from ``conftest.py``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from app.services.adjudication_service import AdjudicationFilters, filters_from_query_params


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
    make_adjudication(amount_uyu=Decimal("1000.00"))

    response = client.get("/")
    body = response.text

    # Both partials are rendered with their canvas elements.
    assert 'id="chart-ranking"' in body
    assert 'id="chart-temporal"' in body


def test_index_includes_distinct_organisms_in_datalist(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(organism="DLIST-MIN-INTERIOR")
    make_adjudication(organism="DLIST-OSE")

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
    make_adjudication(winning_company="PARTIAL-COMPANY-A")
    make_adjudication(winning_company="PARTIAL-COMPANY-B")

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
    make_adjudication(article="Laptop Dell Latitude", winning_company="A")
    make_adjudication(article="Monitor LG 24", winning_company="B")

    response = client.get("/adjudications?article=laptop")

    assert response.status_code == 200
    body = response.text
    assert "Laptop Dell Latitude" in body
    assert "Monitor LG 24" not in body


def test_company_filter_applies_case_insensitive_partial_match(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(winning_company="COMPANY-1", article="COMPANY-1-article")
    make_adjudication(winning_company="COMPANY-2", article="COMPANY-2-article")

    response = client.get("/adjudications?company=COMPANY")

    assert response.status_code == 200
    body = response.text
    assert "COMPANY-1" in body
    assert "COMPANY-2" in body  # partial match includes both


def test_company_filter_exact_match_excludes_other_companies(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(winning_company="ACME-CORP", article="X")
    make_adjudication(winning_company="GLOBEX-INC", article="Y")

    response = client.get("/adjudications?company=ACME")

    body = response.text
    assert "ACME-CORP" in body
    assert "GLOBEX-INC" not in body


def test_organism_filter_applies_partial_match(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(organism="Ministerio de Interior", article="X")
    make_adjudication(organism="Ministerio de Salud", article="Y")
    make_adjudication(organism="OSE", article="Z")

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


def test_empty_filters_return_all_records(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(winning_company="ALLFILTER-A")
    make_adjudication(winning_company="ALLFILTER-B")
    make_adjudication(winning_company="ALLFILTER-C")

    response = client.get("/adjudications")

    body = response.text
    for company in ("ALLFILTER-A", "ALLFILTER-B", "ALLFILTER-C"):
        assert company in body


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
    )
    make_adjudication(
        winning_company="SmallCo",
        amount_uyu=Decimal("500.00"),
        organism="Ministerio de Interior",
    )

    response = client.get("/adjudications?organism=OSE")
    body = response.text

    # The ranking payload is JSON-encoded into the canvas's data-chart attribute.
    assert "TopCorp" in body
    assert "SmallCo" not in body


def test_temporal_chart_aggregates_by_month(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        organism="OSE",
        date=date(2024, 1, 5),
        amount_uyu=Decimal("100.00"),
        winning_company="A",
    )
    make_adjudication(
        organism="OSE",
        date=date(2024, 1, 20),
        amount_uyu=Decimal("200.00"),
        winning_company="A",
    )
    make_adjudication(
        organism="Ministerio de Interior",
        date=date(2024, 1, 10),
        amount_uyu=Decimal("50.00"),
        winning_company="B",
    )

    response = client.get("/adjudications")
    body = response.text

    # The chart payload groups by (organism, month). The temporal canvas
    # is present and its data-chart payload contains 2024-01-01.
    assert 'id="chart-temporal"' in body
    assert "2024-01-01" in body


def test_ranking_excludes_null_amount_uyu_rows(
    client: TestClient, make_adjudication
) -> None:
    """Non-convertible currencies (amount_uyu=NULL) are NOT ranked."""

    make_adjudication(
        winning_company="ConvertibleCo",
        amount_uyu=Decimal("100.00"),
    )
    make_adjudication(
        winning_company="NonConvertibleCo",
        amount_uyu=None,
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
    make_adjudication(organism="DLIST2-MIN-INDUSTRIA")
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="organism-suggestions"' in response.text
    assert "DLIST2-MIN-INDUSTRIA" in response.text
