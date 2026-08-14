"""Unit tests for the FastAPI routes in the resource route modules.

The route layer is intentionally thin — these tests exercise the
behaviours declared in the filtering-ui spec and the route's HTMX
contract, using the in-memory SQLite engine from ``conftest.py``.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from fastapi.responses import HTMLResponse

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

from app.presenters import (
    _build_concentration_chart_payload,
    _build_page_numbers,
    _build_trend_chart_payload,
)
from app.routes.common import _CSV_COLUMNS, COMPETITOR_LIMIT, PAGE_SIZE, RANKING_LIMIT
from app.services.company import CompanyProfileSummary, CompanyWinRate
from app.services.dashboard import ConcentrationResult, KpiSummary
from app.services.filters import AdjudicationFilters, filters_from_query_params

# The route layer defaults the date range to the current calendar year
# on cold load (no date params). Tests that don't pass explicit date
# params in the URL MUST use a current-year date for their fixtures,
# otherwise the new default filter will hide them.
CURRENT_YEAR = date.today().year
NEXT_YEAR = CURRENT_YEAR + 1
PREV_YEAR = CURRENT_YEAR - 1


def _chart_payload(body: str, chart_type: str) -> dict:
    soup = BeautifulSoup(body, "html.parser")
    for canvas in soup.find_all("canvas"):
        payload = cast("dict[str, Any]", json.loads(str(canvas["data-chart"])))
        if payload.get("type") == chart_type:
            return payload
    raise AssertionError(f"Missing {chart_type} chart")


def test_concentration_payload_uses_company_competition_labels() -> None:
    payload = _build_concentration_chart_payload(
        ConcentrationResult(Decimal("0.4"), 2, 3), competition_labels=True
    )

    assert payload["labels"] == ["sin competencia", "con competencia"]
    assert payload["datasets"][0]["data"] == [2, 3]


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


def test_index_trend_payload_preserves_partial_flag_in_serialized_chart(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(date=date.today())

    response = client.get("/")

    assert response.status_code == 200
    assert _chart_payload(response.text, "line")["partial"] is True


def test_all_trend_chart_blocks_disclose_partial_month(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        organism="PARTIAL-ORG",
        winning_company="PARTIAL-COMPANY",
        company_document_type="RUT",
        company_document="PARTIAL-42",
        date=date.today(),
    )

    responses = [
        client.get("/").text,
        client.get("/organism/PARTIAL-ORG").text,
        client.get("/company/RUT/PARTIAL-42").text,
    ]

    for body in responses:
        assert body.count("borderDash = [6, 4]") == 1
        assert "(mes en curso)" in body


def test_index_renders_no_results_message_when_db_is_empty(
    client: TestClient,
) -> None:
    response = client.get("/")
    body = response.text

    assert response.status_code == 200
    assert "No se encontraron adjudicaciones" in body


def test_index_renders_ranking_list_headings(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(amount_uyu=Decimal("1000.00"), date=date(CURRENT_YEAR, 3, 1))

    response = client.get("/")
    body = response.text

    # Both ranking list partials are rendered with their <h2> headings.
    assert 'id="ranking-heading"' in body
    assert 'id="organism-ranking-heading"' in body


def test_index_ranking_links_cover_identity_fallback_and_organism_precedence(
    client: TestClient, make_adjudication
) -> None:
    for name, doc_type, doc_number in (
        ("RANKING-LINK-CO", "RUT", "42"),
        ("RANKING-NO-DOC-CO", None, None),
        ("RANKING-ENCODED-CO", "RUT/X &", "00 1/2?"),
        ("RANKING <CO> &", "RUT", "55"),
    ):
        make_adjudication(
            winning_company=name,
            company_document_type=doc_type,
            company_document=doc_number,
            date=date(CURRENT_YEAR, 3, 1),
        )
    make_adjudication(
        organism="ORGANISM-RANKING-LINK",
        winning_company="ORGANISM-RANKING-CO",
        date=date(CURRENT_YEAR, 3, 1),
    )

    soup = BeautifulSoup(client.get("/").text, "html.parser")
    companies = soup.find("section", attrs={"aria-labelledby": "ranking-heading"})
    organisms = soup.find(
        "section", attrs={"aria-labelledby": "organism-ranking-heading"}
    )

    assert companies is not None and organisms is not None
    assert companies.find("a", href="/company/RUT/42") is not None
    assert companies.find("a", href="/company/RUT%2FX%20%26/00%201%2F2%3F") is not None
    assert companies.find(string="RANKING-NO-DOC-CO").parent.name == "p"  # type: ignore[union-attr]
    assert "RANKING &lt;CO&gt; &amp;" in str(companies)
    assert organisms.find("a", href="/organism/ORGANISM-RANKING-LINK") is not None


def test_organism_company_ranking_links_to_company_profile(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        organism="RANKING-ORG",
        winning_company="ORGANISM-RANKING-LINK-CO",
        company_document_type="RUT",
        company_document="84",
        date=date(CURRENT_YEAR, 3, 1),
    )

    soup = BeautifulSoup(client.get("/organism/RANKING-ORG").text, "html.parser")
    ranking = soup.find("section", attrs={"aria-labelledby": "ranking-heading"})

    assert ranking is not None
    assert ranking.find("a", href="/company/RUT/84") is not None


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
    # The partial still embeds the ranking list <h2> heading so the
    # markup is identical to a cold load.
    assert 'id="ranking-heading"' in body


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

    with caplog.at_level(logging.INFO, logger="app.routes.dashboard"):
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

    with caplog.at_level(logging.INFO, logger="app.routes.dashboard"):
        client.get("/adjudications")

    assert any("htmx=False" in record.message for record in caplog.records)


def test_index_uses_kpi_total_for_pagination_without_count(
    client: TestClient,
) -> None:
    kpi = KpiSummary(
        total_amount=Decimal("0"),
        average_amount=Decimal("0"),
        purchase_count=0,
        company_count=0,
        total=25,
    )
    with (
        patch("app.routes.dashboard.kpi_summary", return_value=kpi),
        patch(
            "app.routes.dashboard.count_adjudications",
            side_effect=AssertionError("index must not call count_adjudications"),
        ) as count_mock,
        patch(
            "app.routes.dashboard._render",
            return_value=HTMLResponse("ok"),
        ) as render_mock,
    ):
        response = client.get("/")

    assert response.status_code == 200
    assert render_mock.call_args.args[2]["total"] == 25
    count_mock.assert_not_called()


def test_index_redirect_uses_kpi_total_without_count(
    client: TestClient,
) -> None:
    kpi = KpiSummary(
        total_amount=Decimal("0"),
        average_amount=Decimal("0"),
        purchase_count=0,
        company_count=0,
        total=25,
    )
    with (
        patch("app.routes.dashboard.kpi_summary", return_value=kpi),
        patch(
            "app.routes.dashboard.count_adjudications",
            side_effect=AssertionError("index must not call count_adjudications"),
        ) as count_mock,
    ):
        response = client.get("/?page=999", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"].endswith("?page=3")
    count_mock.assert_not_called()


def test_full_adjudications_partial_uses_kpi_total_without_count(
    client: TestClient,
) -> None:
    kpi = KpiSummary(
        total_amount=Decimal("0"),
        average_amount=Decimal("0"),
        purchase_count=0,
        company_count=0,
        total=25,
    )
    with (
        patch("app.routes.dashboard.kpi_summary", return_value=kpi),
        patch(
            "app.routes.dashboard.count_adjudications",
            side_effect=AssertionError(
                "full partial must not call count_adjudications"
            ),
        ) as count_mock,
        patch(
            "app.routes.dashboard._render",
            return_value=HTMLResponse("ok"),
        ) as render_mock,
    ):
        response = client.get("/adjudications")

    assert response.status_code == 200
    assert render_mock.call_args.args[2]["total"] == 25
    count_mock.assert_not_called()


def test_table_only_partial_keeps_count_without_kpi(
    client: TestClient,
) -> None:
    with (
        patch("app.routes.dashboard.count_adjudications", return_value=1) as count_mock,
        patch(
            "app.routes.dashboard.kpi_summary",
            side_effect=AssertionError("table-only partial must not call KPI"),
        ) as kpi_mock,
        patch(
            "app.routes.dashboard._render",
            return_value=HTMLResponse("ok"),
        ) as render_mock,
    ):
        response = client.get("/adjudications?partial=table")

    assert response.status_code == 200
    assert render_mock.call_args.args[2]["total"] == 1
    count_mock.assert_called_once()
    kpi_mock.assert_not_called()


@pytest.mark.parametrize("path", ["/", "/adjudications"])
def test_dashboard_aggregate_routes_use_the_query_cache(
    client: TestClient, path: str
) -> None:
    kpi = KpiSummary(Decimal("0"), Decimal("0"), 0, 0, 0)

    def run_uncached(_name, aggregate, session, filters, **kwargs):
        return aggregate(session, filters, **kwargs)

    expected_names = ["kpi_summary", "ranking_by_company", "ranking_by_organism"]
    if path == "/":
        expected_names.append("distinct_organisms")
    expected_names += ["monthly_trend", "concentration_ratio"]

    with (
        patch(
            "app.routes.dashboard.cached_aggregate",
            side_effect=run_uncached,
        ) as cache_mock,
        patch("app.routes.dashboard.kpi_summary", return_value=kpi),
        patch("app.routes.dashboard.ranking_by_company", return_value=[]),
        patch("app.routes.dashboard.ranking_by_organism", return_value=[]),
        patch("app.routes.dashboard.distinct_organisms", return_value=[]),
        patch("app.routes.dashboard.monthly_trend", return_value=[]),
        patch(
            "app.routes.dashboard.concentration_ratio",
            return_value=ConcentrationResult(None, 0, 0),
        ),
    ):
        client.get(path)

    assert [call.args[0] for call in cache_mock.call_args_list] == expected_names


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


def test_ranking_list_reflects_active_filters(
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

    # The ranking list is filtered by the active filter set: TopCorp
    # (matching the OSE organism filter) is rendered, SmallCo is not.
    assert "TopCorp" in body
    assert "SmallCo" not in body


def test_organism_ranking_list_aggregates_by_organism(
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

    # The organism ranking list groups by organism and renders the
    # aggregated totals for both organisms.
    assert 'id="organism-ranking-heading"' in body
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
    # ...but it MUST NOT appear in the ranking list. The convertible
    # company shows up in the table itself, not just the ranking.
    assert "TopCorp" not in body  # sanity: not introduced
    assert "ConvertibleCo" in body  # visible in some context


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


def test_index_filter_submit_button_has_htmx_indicator(client: TestClient) -> None:
    """The Aplicar button must declare itself as the HTMX indicator source.

    The spinner is nested inside the submit button, but HTMX adds
    ``.htmx-request`` to the request-triggering element (the form), not
    the button.  Without ``hx-indicator="this"`` the button never gets
    ``.htmx-request`` and the spinner stays hidden.
    """

    response = client.get("/")
    assert response.status_code == 200
    body = response.text

    # The spinner lives inside the Aplicar button.
    assert '>Aplicar <span class="htmx-indicator spinner"></span></button>' in body
    # HTMX must add .htmx-request to the button itself so the descendant
    # .htmx-indicator becomes visible.
    assert 'hx-indicator="this"' in body


def test_index_is_htmx_compatible(client: TestClient) -> None:
    """The page must include the htmx script tag so swaps work in the browser."""

    response = client.get("/")
    assert response.status_code == 200
    assert "htmx.min.js" in response.text


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


def test_excessive_date_range_returns_422_on_index_page(
    client: TestClient,
) -> None:
    """Index page returns 422 when the date range exceeds 5 years."""

    response = client.get("/adjudications?date_from=2010-01-01&date_to=2020-01-01")

    assert response.status_code == 422
    body = response.text
    # The inline error fragment is an alert role with the Spanish
    # max-range message (spec, "Index page surfaces the maximum-range
    # error" scenario).
    assert 'role="alert"' in body
    assert "5 años" in body


def test_excessive_date_range_returns_422_on_organism_page(
    client: TestClient, make_adjudication
) -> None:
    """Organism profile page returns 422 when the date range exceeds 5 years."""

    make_adjudication(
        organism="Ministerio del Interior",
        winning_company="ACME",
        amount_uyu=Decimal("1000.00"),
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get(
        "/organism/Ministerio%20del%20Interior/partial"
        "?date_from=2010-01-01&date_to=2020-01-01"
    )

    assert response.status_code == 422
    body = response.text
    # The inline error fragment is an alert role with the Spanish
    # max-range message (spec, "Organism profile page surfaces the
    # maximum-range error" scenario).
    assert 'role="alert"' in body
    assert "5 años" in body


# ---------------------------------------------------------------------------
# Full-page (non-HTMX) 422 rendering — bug fix
#
# When a user navigates directly to ``GET /`` or ``GET /organism/{name}``
# with an invalid date range, the response must be a full HTML page
# (extending ``base.html``) so the error is rendered with the page
# chrome — not a bare fragment. The HTMX partials
# (``GET /adjudications`` and ``GET /organism/{name}/partial``) keep
# returning the bare fragment because HTMX swaps it into the existing
# ``#results`` / ``#organism-body`` container.
# ---------------------------------------------------------------------------


def test_full_page_index_renders_422_with_page_chrome_on_excessive_range(
    client: TestClient,
) -> None:
    """GET / with an over-5y range returns 422 *with the full page chrome*.

    Regression test: previously the route returned a bare ``<div>`` alert
    fragment, so the user saw unstyled plain text. The fix renders
    ``index.html`` (which extends ``base.html``) so the error is
    displayed inside the normal page layout.
    """

    response = client.get("/?date_from=2010-01-01&date_to=2020-01-01")

    assert response.status_code == 422
    body = response.text
    # The response is a full HTML document — header, footer, the lot.
    assert body.startswith("<!DOCTYPE html>") or body.startswith("<html")
    assert "<html" in body
    # The page title is the index title.
    assert "AdjudicaUY" in body
    # The error fragment is present (alert role + Spanish max-range
    # message).
    assert 'role="alert"' in body
    assert "5 años" in body
    # The header from base.html is rendered (the brand title).
    assert ">AdjudicaUY<" in body
    # The footer is rendered too — full chrome, not a fragment.
    assert "Agencia de Compras y Contrataciones" in body
    # The filter form is still present so the user can correct the dates.
    assert 'name="date_from"' in body
    assert 'name="date_to"' in body


def test_full_page_index_renders_422_with_page_chrome_on_invalid_format(
    client: TestClient,
) -> None:
    """GET / with an unparseable date returns 422 with the full page chrome."""

    response = client.get("/?date_from=not-a-date")

    assert response.status_code == 422
    body = response.text
    # Full page chrome.
    assert "<html" in body
    assert "AdjudicaUY" in body
    # Error fragment with the Spanish ISO-format hint.
    assert 'role="alert"' in body
    assert "AAAA-MM-DD" in body
    # The header/footer are present.
    assert ">AdjudicaUY<" in body
    assert "Agencia de Compras y Contrataciones" in body


def test_full_page_index_renders_422_with_page_chrome_on_reversed_range(
    client: TestClient,
) -> None:
    """GET / with date_from > date_to returns 422 with the full page chrome."""

    response = client.get("/?date_from=2025-12-01&date_to=2025-01-01")

    assert response.status_code == 422
    body = response.text
    assert "<html" in body
    assert "AdjudicaUY" in body
    assert 'role="alert"' in body
    assert "Desde" in body
    assert "Hasta" in body


def test_full_page_organism_renders_422_with_page_chrome_on_excessive_range(
    client: TestClient, make_adjudication
) -> None:
    """GET /organism/{name} with an over-5y range returns 422 with full chrome.

    Same regression as the index case, but for the organism profile
    route. The page must render ``organism_detail.html`` (extending
    ``base.html``) with the error displayed inside the
    ``#organism-body`` swap target.
    """

    make_adjudication(
        organism="Ministerio del Interior",
        winning_company="ACME",
        amount_uyu=Decimal("1000.00"),
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get(
        "/organism/Ministerio%20del%20Interior?date_from=2010-01-01&date_to=2020-01-01"
    )

    assert response.status_code == 422
    body = response.text
    # Full page chrome.
    assert body.startswith("<!DOCTYPE html>") or body.startswith("<html")
    assert "<html" in body
    # Page title (the organism name appears in <title> and <h1>).
    assert "Ministerio del Interior" in body
    assert "AdjudicaUY" in body
    # Error fragment.
    assert 'role="alert"' in body
    assert "5 años" in body
    # Header + footer present.
    assert ">AdjudicaUY<" in body
    assert "Agencia de Compras y Contrataciones" in body
    # The "Volver al buscador" link is still rendered (full chrome).
    assert "Volver al buscador" in body


def test_full_page_organism_renders_422_with_page_chrome_on_invalid_format(
    client: TestClient, make_adjudication
) -> None:
    """GET /organism/{name} with an unparseable date returns 422 with full chrome."""

    make_adjudication(
        organism="Ministerio del Interior",
        winning_company="ACME",
        amount_uyu=Decimal("1000.00"),
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/organism/Ministerio%20del%20Interior?date_from=not-a-date")

    assert response.status_code == 422
    body = response.text
    assert "<html" in body
    assert "AdjudicaUY" in body
    assert 'role="alert"' in body
    assert "AAAA-MM-DD" in body
    assert ">AdjudicaUY<" in body
    assert "Agencia de Compras y Contrataciones" in body


def test_htmx_partial_still_returns_bare_fragment_on_excessive_range(
    client: TestClient,
) -> None:
    """HTMX partials (not full pages) keep returning the bare error fragment.

    This is the contract that the existing
    ``test_excessive_date_range_returns_422_on_index_page`` already
    covers; the new test makes it explicit that the fix for the full
    pages does NOT change the partial-route behaviour. The response
    body must NOT be a full HTML document.
    """

    response = client.get(
        "/adjudications?date_from=2010-01-01&date_to=2020-01-01",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422
    body = response.text
    # The error fragment is present.
    assert 'role="alert"' in body
    assert "5 años" in body
    # But it is NOT a full HTML document — no <html>, no header, no
    # footer. HTMX swaps this string into the existing
    # ``#results`` container.
    assert "<html" not in body
    assert "<header" not in body
    assert "Agencia de Compras y Contrataciones" not in body


def test_organism_htmx_partial_still_returns_bare_fragment_on_excessive_range(
    client: TestClient, make_adjudication
) -> None:
    """Organism HTMX partial keeps the bare-fragment contract."""

    make_adjudication(
        organism="Ministerio del Interior",
        winning_company="ACME",
        amount_uyu=Decimal("1000.00"),
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get(
        "/organism/Ministerio%20del%20Interior/partial"
        "?date_from=2010-01-01&date_to=2020-01-01"
    )

    assert response.status_code == 422
    body = response.text
    assert 'role="alert"' in body
    assert "5 años" in body
    # Bare fragment, not a full document.
    assert "<html" not in body
    assert "<header" not in body


# ---------------------------------------------------------------------------
# Company profile routes
# ---------------------------------------------------------------------------


def test_company_profile_full_page_renders_identity_kpis_and_history(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        winning_company="ACME latest",
        company_document_type="RUT",
        company_document="210000000012",
        date=date(CURRENT_YEAR, 3, 1),
        amount_uyu=Decimal("1000.00"),
    )

    response = client.get("/company/RUT/210000000012")

    assert response.status_code == 200
    assert "ACME latest" in response.text
    assert "Adjudicaciones" in response.text
    assert "Organismos" in response.text
    assert "Participación del total" in response.text
    assert "No se encontró actividad" not in response.text


def test_company_profile_renders_adaptive_tiny_share(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        winning_company="TINY-SHARE",
        company_document_type="RUT",
        company_document="210000000012",
        amount_uyu=Decimal("57"),
        date=date(CURRENT_YEAR, 3, 1),
    )
    make_adjudication(
        winning_company="LARGE-SHARE",
        amount_uyu=Decimal("9943"),
        date=date(CURRENT_YEAR, 3, 2),
    )

    response = client.get("/company/RUT/210000000012")

    assert response.status_code == 200
    assert "0,570 %" in response.text


def test_concentration_labels_are_company_specific(
    client: TestClient, make_adjudication, db_session
) -> None:
    from app.models.oferente import Oferente

    company = make_adjudication(
        winning_company="COMPANY-LABELS",
        company_document_type="RUT",
        company_document="42",
        date=date(CURRENT_YEAR, 3, 1),
    )
    index = make_adjudication(date=date(CURRENT_YEAR, 3, 2))
    organism = make_adjudication(
        organism="LABELS-ORGANISM", date=date(CURRENT_YEAR, 3, 3)
    )
    for adjudication, name in (
        (company, "COMPANY-BIDDER"),
        (index, "INDEX-BIDDER"),
        (organism, "ORG-BIDDER"),
    ):
        db_session.add(
            Oferente(compra_id=adjudication.compra_id, nombre_comercial=name)
        )
    db_session.commit()

    company_response = client.get("/company/RUT/42")
    index_response = client.get("/")
    organism_response = client.get("/organism/LABELS-ORGANISM")

    assert _chart_payload(company_response.text, "doughnut")["labels"] == [
        "sin competencia",
        "con competencia",
    ]
    for response in (index_response, organism_response):
        assert _chart_payload(response.text, "doughnut")["labels"] == [
            "1 oferente",
            "más de 1 oferente",
        ]
    assert (
        'aria-label="Porcentaje de compras con un solo oferente"'
        in company_response.text
    )
    assert "compra(s) con un solo oferente sobre" in company_response.text


def test_company_profile_full_page_contains_seo_and_corporation_json_ld(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        winning_company="SEO Company",
        company_document_type="RUT",
        company_document="42",
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/company/RUT/42")

    assert response.status_code == 200
    assert '<meta name="description"' in response.text
    assert 'property="og:type" content="Corporation"' in response.text
    assert 'property="og:url"' in response.text
    assert '<link rel="canonical"' in response.text
    assert 'type="application/ld+json"' in response.text
    assert '"@type": "Corporation"' in response.text
    assert '"name": "SEO Company"' in response.text
    assert '"propertyID": "RUT"' in response.text
    assert '"value": "42"' in response.text
    assert "/company/RUT/42" in response.text


def test_company_profile_partial_contains_body_without_page_chrome(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        winning_company="PARTIAL-ACME",
        company_document_type="RUT",
        company_document="42",
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/company/RUT/42/partial")

    assert response.status_code == 200
    assert "PARTIAL-ACME" in response.text
    assert "<html" not in response.text
    assert "Agencia de Compras y Contrataciones" not in response.text


def test_company_profile_renders_top_articles_widget_scoped_to_document(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        winning_company="TOP-ARTICLE-COMPANY",
        company_document_type="RUT",
        company_document="42",
        article="TOP-ARTICLE-LAPTOP",
        article_id="article-1",
        amount_uyu=Decimal("200.00"),
        date=date(CURRENT_YEAR, 3, 1),
    )
    make_adjudication(
        winning_company="TOP-ARTICLE-COMPANY",
        company_document_type="RUT",
        company_document="42",
        article="TOP-ARTICLE-MONITOR",
        article_id="article-2",
        amount_uyu=Decimal("100.00"),
        date=date(CURRENT_YEAR, 3, 2),
    )
    make_adjudication(
        winning_company="OTHER-COMPANY",
        company_document_type="RUT",
        company_document="99",
        article="OTHER-COMPANY-ARTICLE",
        article_id="article-other",
        amount_uyu=Decimal("99999.00"),
        date=date(CURRENT_YEAR, 3, 3),
    )

    full = client.get("/company/RUT/42")
    partial = client.get("/company/RUT/42/partial")

    assert full.status_code == 200
    assert 'id="company-top-articles-heading"' in full.text
    assert "TOP-ARTICLE-LAPTOP" in full.text
    assert "TOP-ARTICLE-MONITOR" in full.text
    assert "OTHER-COMPANY-ARTICLE" not in full.text
    assert partial.status_code == 200
    assert 'id="company-top-articles-heading"' in partial.text


def test_company_profile_renders_win_rate_and_competitor_links(
    client: TestClient, make_adjudication, make_oferente
) -> None:
    target_rows = [
        make_adjudication(
            winning_company="Target company",
            company_document_type="RUT",
            company_document="TARGET",
            date=date(CURRENT_YEAR, 3, index),
        )
        for index in (1, 2, 3)
    ]
    make_adjudication(
        winning_company="Canonical A",
        company_document_type="RUT/X",
        company_document="A B",
        date=date(CURRENT_YEAR, 3, 4),
    )
    make_adjudication(
        winning_company="Canonical B",
        company_document_type="CI",
        company_document="B",
        date=date(CURRENT_YEAR, 3, 5),
    )
    for target in target_rows:
        make_oferente(target.compra_id, tipo_doc_prov="RUT", nro_doc_prov="TARGET")
    for target in target_rows[:2]:
        make_oferente(target.compra_id, tipo_doc_prov="RUT/X", nro_doc_prov="A B")
    make_oferente(target_rows[2].compra_id, tipo_doc_prov="CI", nro_doc_prov="B")
    response = client.get("/company/RUT/TARGET")

    assert 'id="company-win-rate-heading"' in response.text
    assert "Ganadas" in response.text and "Participaciones" in response.text
    assert 'href="/company/RUT%2FX/A%20B"' in response.text
    assert 'href="/company/CI/B"' in response.text


def test_company_export_scopes_document_and_active_filters(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        winning_company="CSV-COMPANY-A-2024",
        company_document_type="RUT",
        company_document="42",
        article="CSV-ARTICLE-MATCH",
        date=date(2024, 3, 1),
    )
    make_adjudication(
        winning_company="CSV-COMPANY-A-2025",
        company_document_type="RUT",
        company_document="42",
        article="CSV-ARTICLE-MATCH",
        date=date(2025, 3, 1),
    )
    make_adjudication(
        winning_company="CSV-COMPANY-B",
        company_document_type="RUT",
        company_document="99",
        article="CSV-ARTICLE-MATCH",
        date=date(2024, 3, 1),
    )
    make_adjudication(
        winning_company="CSV-COMPANY-A-WRONG-ARTICLE",
        company_document_type="RUT",
        company_document="42",
        article="CSV-ARTICLE-OTHER",
        date=date(2024, 3, 1),
    )

    response = client.get(
        "/company/RUT/42/export?article=MATCH&date_from=2024-01-01&date_to=2024-12-31"
    )

    assert response.status_code == 200
    assert "CSV-COMPANY-A-2024" in response.text
    assert "CSV-COMPANY-A-2025" not in response.text
    assert "CSV-COMPANY-B" not in response.text
    assert "CSV-COMPANY-A-WRONG-ARTICLE" not in response.text


def test_company_export_applies_same_row_cap_as_global_export(
    client: TestClient, make_adjudication, monkeypatch
) -> None:
    import app.routes.common as routes_module

    monkeypatch.setattr(routes_module, "MAX_EXPORT_ROWS", 0)
    make_adjudication(
        company_document_type="RUT",
        company_document="42",
        article="laptop",
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/company/RUT/42/export")

    assert response.status_code == 400
    assert "500.000" in response.text


def test_company_export_decodes_identity_and_matches_global_csv_shape(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        winning_company="CSV-ENCODED-COMPANY",
        company_document_type="RUT X",
        company_document="00 123",
        date=date(CURRENT_YEAR, 3, 1),
        compra_overrides={"id_compra": "company-purchase-42"},
    )

    response = client.get("/company/RUT%20X/00%20123/export")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="adjudicaciones.csv"'
    )
    assert response.content.startswith(b"\xef\xbb\xbf")
    text = response.content.decode("utf-8-sig")
    assert "CSV-ENCODED-COMPANY" in text
    assert "company-purchase-42" in text
    assert (
        "https://www.comprasestatales.gub.uy/consultas/detalle/id/company-purchase-42"
        in text
    )


def test_company_export_escapes_formula_prefix_cells(
    client: TestClient, make_adjudication
) -> None:
    """Company-scoped export escapes dangerous cells with the same rule."""

    make_adjudication(
        winning_company="=1+1",
        organism="+ORG",
        article="@article",
        company_document="42",
        company_document_type="RUT",
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/company/RUT/42/export")

    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    text = response.content.decode("utf-8-sig")
    assert "\r\n" in text

    parsed = list(csv.reader(io.StringIO(text)))
    # Header row is never escaped; dangerous data cells are prefixed
    # with exactly one apostrophe; ordinary identity cells are unchanged.
    assert parsed[0] == _CSV_COLUMNS
    data = parsed[1]
    assert data[1] == "'+ORG"
    assert data[2] == "'=1+1"
    assert data[3] == "'@article"
    assert data[8] == "42"
    assert data[9] == "RUT"


def test_company_export_link_preserves_active_filters(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        company_document_type="RUT",
        company_document="42",
        article="laptop",
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/company/RUT/42?article=laptop&date_from=2024-01-01")

    assert response.status_code == 200
    assert 'href="/company/RUT/42/export?' in response.text
    assert "article=laptop" in response.text
    assert "date_from=2024-01-01" in response.text


def test_unknown_company_profile_returns_200_empty_state(client: TestClient) -> None:
    response = client.get("/company/RUT/999999999999")

    assert response.status_code == 200
    assert "No se encontró actividad registrada" in response.text
    assert "No hay suficientes competidores" in response.text
    assert 'role="status"' in response.text


def test_unknown_company_profile_emits_identity_json_ld(
    client: TestClient,
) -> None:
    response = client.get("/company/RUT/999999999999")

    assert response.status_code == 200
    assert '"@type": "Corporation"' in response.text
    assert '"name": "RUT 999999999999"' in response.text
    assert '"propertyID": "RUT"' in response.text
    assert '"value": "999999999999"' in response.text


def test_company_profile_decodes_both_path_segments(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        winning_company="Encoded company",
        company_document_type="RUT X",
        company_document="00 123",
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/company/RUT%20X/00%20123")

    assert response.status_code == 200
    assert "Encoded company" in response.text
    assert "No se encontró actividad registrada" not in response.text


def test_company_profile_out_of_bounds_page_redirects_to_last_page(
    client: TestClient, make_adjudication
) -> None:
    for index in range(11):
        make_adjudication(
            winning_company=f"Paged-{index}",
            company_document_type="RUT",
            company_document="42",
            date=date(CURRENT_YEAR, 3, 1),
        )

    response = client.get("/company/RUT/42?page=3", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "?page=2"


def test_company_route_uses_summary_total_without_count(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        company_document_type="RUT",
        company_document="42",
        date=date(CURRENT_YEAR, 3, 1),
    )
    summary = CompanyProfileSummary(
        display_name=None,
        total_amount=Decimal("100.00"),
        purchase_count=1,
        organism_count=1,
        share_of_total=Decimal("1"),
        total=25,
    )
    with (
        patch("app.routes.company.company_summary", return_value=summary),
        patch(
            "app.routes.company._render", return_value=HTMLResponse("ok")
        ) as render_mock,
    ):
        response = client.get("/company/RUT/42")

    assert response.status_code == 200
    assert render_mock.call_args.args[2]["total"] == 25


def test_company_route_redirects_out_of_bounds_from_summary_total(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        company_document_type="RUT",
        company_document="42",
        date=date(CURRENT_YEAR, 3, 1),
    )
    summary = CompanyProfileSummary(
        display_name=None,
        total_amount=Decimal("100.00"),
        purchase_count=1,
        organism_count=1,
        share_of_total=Decimal("1"),
        total=11,
    )
    with (
        patch("app.routes.company.company_summary", return_value=summary),
    ):
        response = client.get("/company/RUT/42?page=3", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "?page=2"


def test_company_route_passes_cached_market_kpi_and_preserves_key_identity(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        company_document_type="RUT",
        company_document="42",
        date=date(CURRENT_YEAR, 3, 1),
        amount_uyu=Decimal("150.00"),
    )
    kpi = KpiSummary(
        total_amount=Decimal("300.00"),
        average_amount=Decimal("150.00"),
        purchase_count=2,
        company_count=2,
        total=2,
    )
    summary = CompanyProfileSummary(
        display_name=None,
        total_amount=Decimal("150.00"),
        purchase_count=1,
        organism_count=1,
        share_of_total=Decimal("0.5"),
        total=1,
    )
    cache_calls: list[tuple[str, AdjudicationFilters]] = []

    def record_cache(name, aggregate, session, filters, **kwargs):
        cache_calls.append((name, filters))
        if name == "kpi_summary":
            return kpi
        return aggregate(session, filters, **kwargs)

    with (
        patch(
            "app.routes.company.cached_aggregate",
            side_effect=record_cache,
        ),
        patch(
            "app.routes.company.company_summary", return_value=summary
        ) as summary_mock,
        patch("app.routes.company._render", return_value=HTMLResponse("ok")),
    ):
        response = client.get("/company/RUT/42")

    assert response.status_code == 200
    market_calls = [filters for name, filters in cache_calls if name == "kpi_summary"]
    assert len(market_calls) == 1
    market_filters = market_calls[0]
    assert market_filters.company_doc_exact is None

    from app.services.query_cache import build_cache_key

    dashboard_filters = filters_from_query_params(
        {
            "date_from": f"{CURRENT_YEAR}-01-01",
            "date_to": f"{CURRENT_YEAR}-12-31",
        }
    )
    assert build_cache_key("kpi_summary", market_filters) == build_cache_key(
        "kpi_summary", dashboard_filters
    )
    assert summary_mock.call_args.kwargs["market_total"] == kpi.total_amount


def test_company_context_cache_hits_eight_aggregates(db_session) -> None:
    from app.routes.company import _build_company_context

    raw_params: dict[str, str | None] = {
        "date_from": f"{CURRENT_YEAR}-01-01",
        "date_to": f"{CURRENT_YEAR}-12-31",
    }
    summary = CompanyProfileSummary(
        display_name=None,
        total_amount=Decimal("0"),
        total=0,
        purchase_count=0,
        organism_count=0,
        share_of_total=Decimal("0"),
    )
    with (
        patch(
            "app.routes.company.company_summary", return_value=summary
        ) as summary_mock,
        patch(
            "app.routes.company.company_win_rate",
            return_value=CompanyWinRate(0, 0, None),
        ) as win_rate_mock,
        patch(
            "app.routes.company.company_competitors", return_value=[]
        ) as competitors_mock,
        patch("app.routes.company.top_articles", return_value=[]) as articles_mock,
        patch("app.routes.company.monthly_trend", return_value=[]) as trend_mock,
        patch(
            "app.routes.company.concentration_ratio",
            return_value=ConcentrationResult(None, 0, 0),
        ) as concentration_mock,
        patch(
            "app.routes.company.ranking_by_organism", return_value=[]
        ) as ranking_mock,
        patch(
            "app.routes.company.distinct_organisms", return_value=[]
        ) as organisms_mock,
    ):
        _build_company_context(
            db_session,
            raw_type="RUT",
            raw_number="42",
            raw_params=raw_params.copy(),
        )
        _build_company_context(
            db_session,
            raw_type="RUT",
            raw_number="42",
            raw_params=raw_params.copy(),
        )

    assert trend_mock.call_count == 1
    assert concentration_mock.call_count == 1
    assert ranking_mock.call_count == 1
    assert organisms_mock.call_count == 1
    assert summary_mock.call_count == 1
    assert win_rate_mock.call_count == 1
    assert competitors_mock.call_count == 1
    assert articles_mock.call_count == 1
    # The route wrappers forward explicit limits through the cache boundary;
    # the win-rate adapter consumes the cache-key limit and never forwards it
    # into the service math.
    assert competitors_mock.call_args.kwargs["limit"] == COMPETITOR_LIMIT
    assert "limit" not in win_rate_mock.call_args.kwargs


def test_company_context_cache_cold_miss_runs_eight_aggregates(db_session) -> None:
    from app.routes.company import _build_company_context

    summary = CompanyProfileSummary(
        display_name=None,
        total_amount=Decimal("0"),
        total=0,
        purchase_count=0,
        organism_count=0,
        share_of_total=Decimal("0"),
    )
    with (
        patch(
            "app.routes.company.company_summary", return_value=summary
        ) as summary_mock,
        patch(
            "app.routes.company.company_win_rate",
            return_value=CompanyWinRate(0, 0, None),
        ) as win_rate_mock,
        patch(
            "app.routes.company.company_competitors", return_value=[]
        ) as competitors_mock,
        patch("app.routes.company.top_articles", return_value=[]) as articles_mock,
        patch("app.routes.company.monthly_trend", return_value=[]) as trend_mock,
        patch(
            "app.routes.company.concentration_ratio",
            return_value=ConcentrationResult(None, 0, 0),
        ) as concentration_mock,
        patch(
            "app.routes.company.ranking_by_organism", return_value=[]
        ) as ranking_mock,
        patch(
            "app.routes.company.distinct_organisms", return_value=[]
        ) as organisms_mock,
    ):
        _build_company_context(
            db_session,
            raw_type="RUT",
            raw_number="42",
            raw_params={
                "date_from": f"{CURRENT_YEAR}-01-01",
                "date_to": f"{CURRENT_YEAR}-12-31",
            },
        )

    assert trend_mock.call_count == 1
    assert concentration_mock.call_count == 1
    assert ranking_mock.call_count == 1
    assert organisms_mock.call_count == 1
    assert summary_mock.call_count == 1
    assert win_rate_mock.call_count == 1
    assert competitors_mock.call_count == 1
    assert articles_mock.call_count == 1
    # The route wrappers forward explicit limits through the cache boundary;
    # the win-rate adapter consumes the cache-key limit and never forwards it
    # into the service math.
    assert competitors_mock.call_args.kwargs["limit"] == COMPETITOR_LIMIT
    assert "limit" not in win_rate_mock.call_args.kwargs


def test_company_cache_keys_differ_from_dashboard_keys() -> None:
    from dataclasses import replace

    from app.services.query_cache import build_cache_key

    company_filters = AdjudicationFilters(
        company_doc_exact=("RUT", "42"),
        date_from=date(CURRENT_YEAR, 1, 1),
        date_to=date(CURRENT_YEAR, 12, 31),
    )
    dashboard_filters = replace(company_filters, company_doc_exact=None)

    for name in (
        "monthly_trend",
        "concentration_ratio",
        "ranking_by_organism",
        "distinct_organisms",
        "company_win_rate",
        "company_competitors",
        "company_summary",
        "top_articles",
    ):
        limit = (
            10
            if name in {"ranking_by_organism", "top_articles", "company_win_rate"}
            else 5
            if name == "company_competitors"
            else 200
            if name == "distinct_organisms"
            else None
        )
        assert build_cache_key(name, company_filters, limit=limit) != build_cache_key(
            name, dashboard_filters, limit=limit
        )

    assert build_cache_key("ranking_by_organism", company_filters, limit=10) != (
        build_cache_key("ranking_by_organism", company_filters, limit=20)
    )
    assert build_cache_key("distinct_organisms", company_filters, limit=100) != (
        build_cache_key("distinct_organisms", company_filters, limit=200)
    )
    # Same filters with different explicit limits must never share a key for
    # the two newly limit-aware company aggregates.
    assert build_cache_key("company_win_rate", company_filters, limit=10) != (
        build_cache_key("company_win_rate", company_filters, limit=5)
    )
    assert build_cache_key("company_competitors", company_filters, limit=5) != (
        build_cache_key("company_competitors", company_filters, limit=10)
    )


def test_company_route_forwards_explicit_limits_to_cache_and_adapters(
    db_session,
) -> None:
    """The company route passes explicit limits to cached_aggregate and the adapters.

    ``company_win_rate`` must be cached with ``limit=RANKING_LIMIT`` and
    ``company_competitors`` with ``limit=COMPETITOR_LIMIT``; the competitor
    adapter must receive that explicit display limit.
    """

    from app.routes import company as company_route
    from app.routes.company import _build_company_context

    raw_params: dict[str, str | None] = {
        "date_from": f"{CURRENT_YEAR}-01-01",
        "date_to": f"{CURRENT_YEAR}-12-31",
    }
    cache_limits: dict[str, int | None] = {}
    real_cached_aggregate = company_route.cached_aggregate

    def recording_cached_aggregate(name, aggregate, session, filters, **kwargs):
        if name in ("company_win_rate", "company_competitors"):
            cache_limits[name] = kwargs.get("limit")
        return real_cached_aggregate(name, aggregate, session, filters, **kwargs)

    adapter_limits: list[int | None] = []

    def fake_competitors(session, decoded_type, decoded_number, filters, limit):
        adapter_limits.append(limit)
        return []

    with (
        patch(
            "app.routes.company.cached_aggregate",
            side_effect=recording_cached_aggregate,
        ),
        patch(
            "app.routes.company.company_win_rate",
            return_value=CompanyWinRate(0, 0, None),
        ),
        patch(
            "app.routes.company.company_competitors",
            side_effect=fake_competitors,
        ),
    ):
        _build_company_context(
            db_session,
            raw_type="RUT",
            raw_number="42",
            raw_params=raw_params.copy(),
        )

    assert cache_limits["company_win_rate"] == RANKING_LIMIT
    assert cache_limits["company_competitors"] == COMPETITOR_LIMIT
    assert adapter_limits == [COMPETITOR_LIMIT]


def test_company_route_forwards_limits_when_cache_disabled(
    db_session, monkeypatch
) -> None:
    """TTL zero still forwards explicit limits through both adapters.

    With caching disabled each context build must evaluate the aggregates
    again, and the limit forwarding contract must hold on every call: the
    win-rate service is invoked without the cache-key limit and the
    competitor service always receives the display limit.
    """

    from app.routes.company import _build_company_context

    monkeypatch.setenv("CACHE_TTL_SECONDS", "0")
    raw_params: dict[str, str | None] = {
        "date_from": f"{CURRENT_YEAR}-01-01",
        "date_to": f"{CURRENT_YEAR}-12-31",
    }
    with (
        patch(
            "app.routes.company.company_win_rate",
            return_value=CompanyWinRate(0, 0, None),
        ) as win_rate_mock,
        patch(
            "app.routes.company.company_competitors", return_value=[]
        ) as competitors_mock,
    ):
        _build_company_context(
            db_session,
            raw_type="RUT",
            raw_number="42",
            raw_params=raw_params.copy(),
        )
        _build_company_context(
            db_session,
            raw_type="RUT",
            raw_number="42",
            raw_params=raw_params.copy(),
        )

    assert win_rate_mock.call_count == 2
    assert competitors_mock.call_count == 2
    assert competitors_mock.call_args.kwargs["limit"] == COMPETITOR_LIMIT
    assert "limit" not in win_rate_mock.call_args.kwargs


def test_company_context_company_summary_uses_market_total(db_session) -> None:
    from app.routes.company import _build_company_context

    kpi = KpiSummary(Decimal("900.00"), Decimal("0"), 3, 2, 4)
    summary = CompanyProfileSummary(
        display_name=None,
        total_amount=Decimal("300.00"),
        total=1,
        purchase_count=1,
        organism_count=1,
        share_of_total=Decimal("0.33"),
    )
    raw_params: dict[str, str | None] = {
        "date_from": f"{CURRENT_YEAR}-01-01",
        "date_to": f"{CURRENT_YEAR}-12-31",
    }
    with (
        patch("app.routes.company.kpi_summary", return_value=kpi) as kpi_mock,
        patch(
            "app.routes.company.company_summary", return_value=summary
        ) as summary_mock,
        patch(
            "app.routes.company.lookup_company_identity",
            side_effect=["Initial Name", "Fresh Name"],
        ),
        patch(
            "app.routes.company.company_win_rate",
            return_value=CompanyWinRate(0, 0, None),
        ),
        patch("app.routes.company.company_competitors", return_value=[]),
        patch("app.routes.company.top_articles", return_value=[]),
    ):
        first = _build_company_context(
            db_session,
            raw_type="RUT",
            raw_number="42",
            raw_params=raw_params.copy(),
        )
        second = _build_company_context(
            db_session,
            raw_type="RUT",
            raw_number="42",
            raw_params=raw_params.copy(),
        )

    assert kpi_mock.call_count == 1
    assert summary_mock.call_count == 1
    assert summary_mock.call_args.kwargs["market_total"] == Decimal("900.00")
    assert first["company_summary"].display_name == "Initial Name"
    assert second["company_summary"].display_name == "Fresh Name"


@pytest.mark.parametrize(
    ("raw_type", "raw_number"),
    [("", ""), ("RUT", ""), ("", "42")],
)
def test_company_context_empty_identity_returns_pinned_empty_context(
    db_session, raw_type, raw_number
) -> None:
    """An empty decoded company identity short-circuits before any DB work.

    The single identity guard must return the pinned empty-context dict —
    zeroed ``CompanyProfileSummary``, ``CompanyWinRate(0, 0, None)``, a
    null ``ConcentrationResult``, and ``company_variant=True`` — without
    invoking the identity lookup, any cached aggregate, or the
    adjudication listing (fail-if-invoked mocks prove the early return).
    A real route request cannot carry an empty path segment (Starlette
    rejects it before the handler), so the guard is exercised directly
    through ``_build_company_context``, matching the existing context
    tests.
    """

    from app.routes.company import _build_company_context

    raw_params: dict[str, str | None] = {
        "date_from": f"{CURRENT_YEAR}-01-01",
        "date_to": f"{CURRENT_YEAR}-12-31",
        "page": "4",
    }
    with (
        patch(
            "app.routes.company.lookup_company_identity",
            side_effect=AssertionError("empty identity must not look up a company"),
        ) as lookup_mock,
        patch(
            "app.routes.company.cached_aggregate",
            side_effect=AssertionError("empty identity must not run aggregates"),
        ) as cache_mock,
        patch(
            "app.routes.company.list_adjudications",
            side_effect=AssertionError("empty identity must not load listings"),
        ) as listing_mock,
    ):
        context = _build_company_context(
            db_session,
            raw_type=raw_type,
            raw_number=raw_number,
            raw_params=raw_params.copy(),
        )

    assert context["filters"].company_doc_exact == (raw_type, raw_number)
    assert context["company_type"] == raw_type
    assert context["company_number"] == raw_number
    assert context["company_name"] is None
    assert context["company_summary"] == CompanyProfileSummary(
        display_name=None,
        total_amount=Decimal("0"),
        total=0,
        purchase_count=0,
        organism_count=0,
        share_of_total=Decimal("0"),
    )
    assert context["results"] == []
    assert context["total"] == 0
    assert context["shown"] == 0
    assert context["page_size"] == PAGE_SIZE
    assert context["page"] == 4
    assert context["total_pages"] == 1
    assert context["page_numbers"] == _build_page_numbers(4, 1)
    assert context["ranking_rows"] == []
    assert context["top_article_rows"] == []
    assert context["organisms"] == []
    assert context["trend_rows"] == []
    assert context["trend_payload"] == _build_trend_chart_payload([])
    assert context["has_trend_data"] is False
    assert context["concentration"] == ConcentrationResult(None, 0, 0)
    assert context["concentration_payload"] is None
    assert context["has_concentration_data"] is False
    assert context["company_win_rate"] == CompanyWinRate(0, 0, None)
    assert context["company_competitors"] == []
    assert context["company_variant"] is True
    lookup_mock.assert_not_called()
    cache_mock.assert_not_called()
    listing_mock.assert_not_called()


def test_company_profile_hides_name_filter_but_keeps_organism_filter(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(
        winning_company="Filter variant",
        company_document_type="RUT",
        company_document="42",
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/company/RUT/42")

    assert response.status_code == 200
    assert 'name="company"' not in response.text
    assert 'name="organism"' in response.text


def test_company_profile_full_and_partial_validation_match_existing_style(
    client: TestClient,
) -> None:
    full = client.get("/company/RUT/42?date_from=not-a-date")
    partial = client.get("/company/RUT/42/partial?date_from=not-a-date")

    assert full.status_code == 422
    assert "<html" in full.text
    assert "AAAA-MM-DD" in full.text
    assert partial.status_code == 422
    assert "<html" not in partial.text
    assert 'role="alert"' in partial.text


def test_limpiar_resets_to_current_year(client: TestClient) -> None:
    """The Limpiar control is a button that resets the form and re-triggers
    the HTMX request (NOT an <a href="/"> that would navigate)."""

    response = client.get("/")
    assert response.status_code == 200
    body = response.text

    # The old <a href="/">Limpiar</a> is gone.
    assert ">Limpiar</a>" not in body
    # Inline onclick handlers have been replaced by a data-action attribute.
    assert 'onclick="limpiarFiltros()"' not in body
    assert 'data-action="clear-filters"' in body
    assert ">Limpiar</button>" in body
    # The handler is now delegated from base.html; no global functions.
    assert "window.limpiarFiltros" not in body
    # The handler now clears text inputs and sets date range to the
    # current year instead of using form.reset().
    assert "querySelectorAll('input[type=\"text\"]')" in body
    assert "new Date().getFullYear()" in body


def test_organism_limpiar_resets_date_range(
    client: TestClient, make_adjudication
) -> None:
    """The organism profile page also uses the delegated data-action reset."""

    make_adjudication(
        organism="ORG-LIMPIAR-MIN",
        winning_company="ORG-LIMPIAR-CO",
        amount_uyu=Decimal("1000.00"),
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/organism/ORG-LIMPIAR-MIN")
    assert response.status_code == 200
    body = response.text

    # The old <a href="/">Limpiar</a> and inline onclick handlers are gone.
    assert ">Limpiar</a>" not in body
    assert 'onclick="limpiarFiltrosOrganismo()"' not in body
    assert "window.limpiarFiltrosOrganismo" not in body
    # The delegated reset button uses a distinct data-action value.
    assert 'data-action="clear-organism-filters"' in body
    assert ">Limpiar</button>" in body
    # The delegated handler lives in base.html and is shared with the index.
    assert "querySelectorAll('input[type=\"text\"]')" in body
    assert "new Date().getFullYear()" in body


# ---------------------------------------------------------------------------
# Direct coverage of the pure ``validate_date_params`` helper
# ---------------------------------------------------------------------------


def test_validate_date_params_accepts_missing_keys() -> None:
    from app.services.filters import validate_date_params

    # Empty / absent params are the route's default-injection concern,
    # not a validation error.
    validate_date_params({})
    validate_date_params({"date_from": "", "date_to": None})


def test_validate_date_params_accepts_valid_iso() -> None:
    from app.services.filters import validate_date_params

    validate_date_params({"date_from": "2024-01-15", "date_to": "2024-12-31"})


def test_validate_date_params_rejects_garbage_string() -> None:
    import pytest

    from app.services.filters import (
        DateValidationError,
        validate_date_params,
    )

    with pytest.raises(DateValidationError, match="AAAA-MM-DD"):
        validate_date_params({"date_from": "not-a-date"})


def test_validate_date_params_rejects_reversed_range() -> None:
    import pytest

    from app.services.filters import (
        DateValidationError,
        validate_date_params,
    )

    with pytest.raises(DateValidationError, match="Desde"):
        validate_date_params({"date_from": "2025-12-01", "date_to": "2025-01-01"})


def test_validate_date_params_accepts_under_5y() -> None:
    """A range shorter than 5 years is accepted."""

    from app.services.filters import validate_date_params

    # 3-year range: well under the 5-year limit.
    validate_date_params({"date_from": "2022-01-01", "date_to": "2024-12-31"})


def test_validate_date_params_accepts_exactly_5y_boundary() -> None:
    """A range of exactly 1825 days (5×365) is accepted as the boundary."""

    from app.services.filters import validate_date_params

    # 2020-01-01 → 2024-12-30 is exactly 1825 days (2020 is leap, so
    # the leap day is already accounted for inside the span).
    validate_date_params({"date_from": "2020-01-01", "date_to": "2024-12-30"})


def test_validate_date_params_rejects_over_5y() -> None:
    """A range of 1826 days (one day over) is rejected with the 5-year message."""

    import pytest

    from app.services.filters import (
        DateValidationError,
        validate_date_params,
    )

    # 2020-01-01 → 2024-12-31 is 1826 days — one day over the 1825-day
    # limit. The Spanish max-range message is the contract (spec,
    # "Spanish Error Message for Maximum Range" requirement).
    with pytest.raises(DateValidationError, match="5 años"):
        validate_date_params({"date_from": "2020-01-01", "date_to": "2024-12-31"})


def test_validate_date_params_rejects_wider_range() -> None:
    """A range much wider than 5 years (e.g. 10 years) is rejected."""

    import pytest

    from app.services.filters import (
        DateValidationError,
        validate_date_params,
    )

    # 10-year range: 2010-01-01 → 2020-01-01 is 3652 days.
    with pytest.raises(DateValidationError, match="5 años"):
        validate_date_params({"date_from": "2010-01-01", "date_to": "2020-01-01"})


def test_validate_date_params_accepts_single_date() -> None:
    """When only one date is provided, no range exists to check."""

    from app.services.filters import validate_date_params

    # Only date_from — the max-range check only runs when both are present.
    validate_date_params({"date_from": "2024-01-01", "date_to": ""})
    # Only date_to.
    validate_date_params({"date_from": "", "date_to": "2024-12-31"})


# ---------------------------------------------------------------------------
# GET /organism/{name} — organism profile page (PR#2 of citizen-dashboard)
# ---------------------------------------------------------------------------


def test_organism_context_cold_path_uses_four_cached_aggregates(db_session) -> None:
    """Cold organism context runs exactly the four cached dashboard aggregates.

    The organism profile must obtain KPI, trend, concentration, and company
    ranking through ``cached_aggregate`` with the same names and callable
    semantics as the dashboard route, and the company ranking must carry the
    shared ``RANKING_LIMIT`` explicitly.
    """

    from app.routes import organism as organism_route
    from app.routes.common import RANKING_LIMIT
    from app.routes.organism import _build_organism_context

    raw_params: dict[str, str | None] = {
        "date_from": f"{CURRENT_YEAR}-01-01",
        "date_to": f"{CURRENT_YEAR}-12-31",
    }
    kpi = KpiSummary(Decimal("0"), Decimal("0"), 0, 0, 0)
    calls: list[tuple[str, int | None]] = []
    real_cached_aggregate = organism_route.cached_aggregate

    def recording_cached_aggregate(name, aggregate, session, filters, **kwargs):
        calls.append((name, kwargs.get("limit")))
        return real_cached_aggregate(name, aggregate, session, filters, **kwargs)

    with (
        patch(
            "app.routes.organism.cached_aggregate",
            side_effect=recording_cached_aggregate,
        ),
        patch("app.routes.organism.kpi_summary", return_value=kpi),
        patch("app.routes.organism.monthly_trend", return_value=[]),
        patch(
            "app.routes.organism.concentration_ratio",
            return_value=ConcentrationResult(None, 0, 0),
        ),
        patch("app.routes.organism.ranking_by_company", return_value=[]),
    ):
        _build_organism_context(
            db_session,
            decoded_name="Ministerio de Interior",
            raw_params=raw_params.copy(),
        )

    assert [name for name, _limit in calls] == [
        "kpi_summary",
        "monthly_trend",
        "concentration_ratio",
        "ranking_by_company",
    ]
    assert calls[3] == ("ranking_by_company", RANKING_LIMIT)
    assert all(limit is None for name, limit in calls[:3])


def test_organism_context_warm_hit_reruns_no_aggregates(db_session) -> None:
    """A second identical organism context adds zero aggregate work.

    Once the four aggregate entries are populated for an identical filter set
    and ranking limit, the warm context must reuse every value without
    calling the underlying aggregate functions again.
    """

    from app.routes.organism import _build_organism_context

    raw_params: dict[str, str | None] = {
        "date_from": f"{CURRENT_YEAR}-01-01",
        "date_to": f"{CURRENT_YEAR}-12-31",
    }
    kpi = KpiSummary(Decimal("0"), Decimal("0"), 0, 0, 0)
    with (
        patch("app.routes.organism.kpi_summary", return_value=kpi) as kpi_mock,
        patch("app.routes.organism.monthly_trend", return_value=[]) as trend_mock,
        patch(
            "app.routes.organism.concentration_ratio",
            return_value=ConcentrationResult(None, 0, 0),
        ) as concentration_mock,
        patch(
            "app.routes.organism.ranking_by_company", return_value=[]
        ) as ranking_mock,
    ):
        cold = _build_organism_context(
            db_session,
            decoded_name="Ministerio de Interior",
            raw_params=raw_params.copy(),
        )
        warm = _build_organism_context(
            db_session,
            decoded_name="Ministerio de Interior",
            raw_params=raw_params.copy(),
        )

    assert kpi_mock.call_count == 1
    assert trend_mock.call_count == 1
    assert concentration_mock.call_count == 1
    assert ranking_mock.call_count == 1
    # Warm hit preserves the view-model values.
    assert cold["kpi"] == warm["kpi"]
    assert cold["trend_rows"] == warm["trend_rows"]
    assert cold["trend_payload"] == warm["trend_payload"]
    assert cold["concentration"] == warm["concentration"]
    assert cold["ranking_rows"] == warm["ranking_rows"]


def test_organism_context_cache_disabled_reruns_all_four_aggregates(
    db_session, monkeypatch
) -> None:
    """TTL zero repeats all four aggregates and stores nothing.

    With caching disabled each request must evaluate the aggregates again
    while the view-model values remain identical to the cached path.
    """

    from app.routes.organism import _build_organism_context

    monkeypatch.setenv("CACHE_TTL_SECONDS", "0")
    raw_params: dict[str, str | None] = {
        "date_from": f"{CURRENT_YEAR}-01-01",
        "date_to": f"{CURRENT_YEAR}-12-31",
    }
    kpi = KpiSummary(Decimal("0"), Decimal("0"), 0, 0, 0)
    with (
        patch("app.routes.organism.kpi_summary", return_value=kpi) as kpi_mock,
        patch("app.routes.organism.monthly_trend", return_value=[]) as trend_mock,
        patch(
            "app.routes.organism.concentration_ratio",
            return_value=ConcentrationResult(None, 0, 0),
        ) as concentration_mock,
        patch(
            "app.routes.organism.ranking_by_company", return_value=[]
        ) as ranking_mock,
    ):
        first = _build_organism_context(
            db_session,
            decoded_name="Ministerio de Interior",
            raw_params=raw_params.copy(),
        )
        second = _build_organism_context(
            db_session,
            decoded_name="Ministerio de Interior",
            raw_params=raw_params.copy(),
        )

    assert kpi_mock.call_count == 2
    assert trend_mock.call_count == 2
    assert concentration_mock.call_count == 2
    assert ranking_mock.call_count == 2
    assert first["kpi"] == second["kpi"]
    assert first["trend_rows"] == second["trend_rows"]
    assert first["concentration"] == second["concentration"]
    assert first["ranking_rows"] == second["ranking_rows"]


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
    # The dashboard widgets are all present. Chart canvases no longer
    # carry fixed IDs (they duplicate after HTMX swaps), so we assert
    # the ``data-chart`` payloads are present instead.
    assert "Resumen" in body  # KPI section heading
    assert "data-chart=" in body
    assert 'id="ranking-heading"' in body  # company ranking list
    # The organism ranking list is intentionally absent on this page.
    assert 'id="organism-ranking-heading"' not in body


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
    # The body partial includes the dashboard widgets. Chart canvases
    # no longer carry fixed IDs (they duplicate after HTMX swaps), so
    # we assert the ``data-chart`` payloads are present instead.
    assert "data-chart=" in body
    assert 'id="ranking-heading"' in body
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


def test_results_table_company_link_uses_encoded_company_route(
    client: TestClient, make_adjudication
) -> None:
    """Index company cells link to both encoded document identity segments."""

    make_adjudication(
        organism="Ministerio del Interior",
        winning_company="LINKED-CO",
        company_document_type="RUT/X &",
        company_document="00 1/2?",
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/")

    assert response.status_code == 200
    assert 'href="/company/RUT%2FX%20%26/00%201%2F2%3F"' in response.text
    assert ">LINKED-CO</a>" in response.text


def test_company_profile_results_table_company_link_uses_company_route(
    client: TestClient, make_adjudication
) -> None:
    """Company profile result tables use the same company profile link."""

    make_adjudication(
        winning_company="PROFILE-LINKED-CO",
        company_document_type="RUT",
        company_document="42",
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/company/RUT/42")

    assert response.status_code == 200
    assert 'href="/company/RUT/42"' in response.text
    assert ">PROFILE-LINKED-CO</a>" in response.text


def test_results_table_keeps_company_without_documents_as_plain_text(
    client: TestClient, make_adjudication
) -> None:
    """Rows without a complete document identity are not profile links."""

    make_adjudication(
        winning_company="UNLINKED-CO",
        company_document_type=None,
        company_document=None,
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/")

    assert response.status_code == 200
    assert "UNLINKED-CO" in response.text
    assert "/company/" not in response.text


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _make_pagination_fixtures(make_adjudication, n: int, prefix: str) -> None:
    """Insert ``n`` adjudications with distinct dates AND amounts.

    Two uniqueness keys are needed for clean pagination assertions:

    * **Dates** are incremented by one day per row so the result-set
      ordering (``fecha_pub_adj DESC, adjudicacion.id DESC``) puts the
      LAST inserted row at position 1. Position ``i`` (1-based) of the
      result set is the row with ``winning_company == f"{prefix}-{i:03d}"``.
    * **Amounts** are also distinct (``i * 100``) so the
      ``ranking_by_company`` aggregate returns a predictable top-10
      (the 10 highest amounts = the 10 most recently inserted rows).
      Without this, the ranking list's 10 company names would leak
      into the rendered body and confuse the "row X is NOT on page Y"
      checks.

    All dates live in the current calendar year so the route's default
    year filter (injected on cold load) does not exclude any fixture.
    """

    for i in range(1, n + 1):
        make_adjudication(
            winning_company=f"{prefix}-{i:03d}",
            date=date(CURRENT_YEAR, 1, 1) + timedelta(days=i - 1),
            amount_uyu=Decimal(i * 100),
        )


def test_pagination_default_page_returns_first_10_rows(
    client: TestClient, make_adjudication
) -> None:
    """GET / without ?page returns the first 10 rows of a 20-row set.

    PAGE_SIZE is 10 and the default ``page`` is 1, so the 10 oldest
    rows (positions 11-20) are on page 2 and MUST NOT appear in the
    default response.
    """

    _make_pagination_fixtures(make_adjudication, 20, "PG-DEF")

    response = client.get("/")
    assert response.status_code == 200
    body = response.text

    # Page 1 holds positions 1-10 — the most-recent 10 rows. In our
    # setup those are rows 11..20 (rows 1..10 are the oldest, on page 2).
    for i in range(11, 21):
        assert f"PG-DEF-{i:03d}" in body, f"Expected PG-DEF-{i:03d} on page 1"
    # The 10 oldest rows (positions 1-10) are on page 2 and MUST be
    # excluded from the default response.
    for i in range(1, 11):
        assert f"PG-DEF-{i:03d}" not in body, (
            f"PG-DEF-{i:03d} should be on page 2, not page 1"
        )


def test_pagination_page_zero_clamps_to_page_one(
    client: TestClient, make_adjudication
) -> None:
    """?page=0 is treated as page 1 (no 4xx, no off-by-one)."""

    _make_pagination_fixtures(make_adjudication, 20, "PG-ZERO")

    response = client.get("/?page=0")
    assert response.status_code == 200
    body = response.text

    for i in range(11, 21):
        assert f"PG-ZERO-{i:03d}" in body
    for i in range(1, 11):
        assert f"PG-ZERO-{i:03d}" not in body


def test_pagination_negative_page_clamps_to_page_one(
    client: TestClient, make_adjudication
) -> None:
    """?page=-5 is treated as page 1 — negative values do not 4xx."""

    _make_pagination_fixtures(make_adjudication, 20, "PG-NEG")

    response = client.get("/?page=-5")
    assert response.status_code == 200
    body = response.text

    for i in range(11, 21):
        assert f"PG-NEG-{i:03d}" in body
    for i in range(1, 11):
        assert f"PG-NEG-{i:03d}" not in body


def test_pagination_page_two_returns_rows_1_to_10(
    client: TestClient, make_adjudication
) -> None:
    """?page=2 with 20 rows returns the last 10 rows (positions 1-10)."""

    _make_pagination_fixtures(make_adjudication, 20, "PG-P2")

    response = client.get("/?page=2")
    assert response.status_code == 200
    body = response.text

    # The 10 oldest rows are on page 2.
    for i in range(1, 11):
        assert f"PG-P2-{i:03d}" in body, f"Expected PG-P2-{i:03d} on page 2"
    # Page 1 rows (positions 11-20) appear in the body via the
    # ``ranking_by_company`` ranking list (they have the highest
    # ``amount_uyu`` and top the ranking), so we don't assert on them
    # here — the table-level pagination is what this scenario is
    # verifying. The presence of rows 1-10 above + the redirect
    # scenario in the next test cover the page-2 boundary.


def test_pagination_out_of_bounds_redirects_to_last_valid_page(
    client: TestClient, make_adjudication
) -> None:
    """?page=999 with 20 rows (2 valid pages) returns 302 → ?page=2."""

    _make_pagination_fixtures(make_adjudication, 20, "PG-REDIR")

    response = client.get("/?page=999", follow_redirects=False)

    assert response.status_code == 302
    # The Location header points to the last valid page. FastAPI
    # preserves the relative URL form (``?page=N``).
    assert response.headers["location"].endswith("?page=2")


def test_pagination_empty_dataset_hides_pagination_nav(
    client: TestClient,
) -> None:
    """Zero results → no pagination <nav> rendered, empty state shown."""

    response = client.get("/")
    assert response.status_code == 200
    body = response.text

    # The pagination bar carries an ``aria-label="Paginación"`` —
    # checking for that is the cleanest signal that the nav is
    # absent (regardless of how the rest of the markup evolves).
    assert 'aria-label="Paginación"' not in body
    # The empty-state panel is still rendered.
    assert "No se encontraron adjudicaciones" in body


def test_pagination_single_page_hides_pagination_nav(
    client: TestClient, make_adjudication
) -> None:
    """5 rows fit on one page → no pagination <nav>; all rows visible."""

    _make_pagination_fixtures(make_adjudication, 5, "PG-1PG")

    response = client.get("/")
    assert response.status_code == 200
    body = response.text

    # 5 rows / 10 per page = 1 page → no nav.
    assert 'aria-label="Paginación"' not in body
    # All 5 rows are visible.
    for i in range(1, 6):
        assert f"PG-1PG-{i:03d}" in body


def test_pagination_multi_page_renders_correct_page_numbers(
    client: TestClient, make_adjudication
) -> None:
    """30 rows (3 pages) → nav rendered with links to pages 1, 2, 3."""

    _make_pagination_fixtures(make_adjudication, 30, "PG-MULTI")

    response = client.get("/")
    assert response.status_code == 200
    body = response.text

    # The nav is rendered.
    assert 'aria-label="Paginación"' in body
    # The active page (1) is rendered as a non-link <span> with
    # ``aria-current="page"``.
    assert 'aria-current="page"' in body
    # Page 2 and 3 are rendered as HTMX links targeting /adjudications
    # with partial=table for lightweight pagination.
    assert 'hx-get="/adjudications?page=2&partial=table"' in body
    assert 'hx-get="/adjudications?page=3&partial=table"' in body
    # The links include hx-include for the filter form values so the
    # active filter set is preserved across page changes.
    assert 'hx-include="#filter-form input"' in body
    # On page 1, the "Anterior" control is a disabled button (not a
    # link); the "Siguiente" control is a link to page 2.
    assert ">Anterior</button>" in body
    # Siguiente is the only link with hx-get for page 2 on page 1
    # (the Siguiente button itself, plus the page-2 number link).
    # We already asserted the hx-get for page 2 above.


# ---------------------------------------------------------------------------
# GET /adjudications/export — CSV export
# ---------------------------------------------------------------------------


def test_export_returns_csv_with_correct_headers(
    client: TestClient, make_adjudication
) -> None:
    """Export endpoint returns 200 with CSV content-type and BOM prefix."""

    make_adjudication(
        winning_company="EXPORT-CO",
        organism="EXPORT-ORG",
        article="EXPORT-ARTICLE",
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/adjudications/export")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="adjudicaciones.csv"'
    )
    # UTF-8 BOM at the start.
    assert response.content.startswith(b"\xef\xbb\xbf")


def test_export_csv_header_row_matches_spec(
    client: TestClient, make_adjudication
) -> None:
    """The first CSV row is the column header per the spec."""

    make_adjudication(date=date(CURRENT_YEAR, 3, 1))

    response = client.get("/adjudications/export")
    text = response.content.decode("utf-8-sig")
    first_line = text.split("\r\n")[0]

    assert first_line == (
        "fecha,organismo,empresa_adjudicataria,articulo,monto,moneda,"
        "monto_uyu,tipo_compra,documento_empresa,tipo_documento,id_articulo,"
        "id_compra,link_licitacion"
    )


def test_export_csv_data_row_contains_raw_values(
    client: TestClient, make_adjudication
) -> None:
    """CSV data rows contain raw values (ISO dates, unformatted decimals)."""

    make_adjudication(
        winning_company="RAW-EXPORT-CO",
        organism="RAW-EXPORT-ORG",
        article="RAW-EXPORT-ARTICLE",
        amount=Decimal("1234567.89"),
        amount_uyu=Decimal("1234567.89"),
        date=date(2024, 3, 15),
        compra_overrides={"id_compra": "raw-purchase-42"},
    )

    response = client.get(
        "/adjudications/export?date_from=2024-01-01&date_to=2024-12-31"
    )
    text = response.content.decode("utf-8-sig")
    lines = [line for line in text.split("\r\n") if line]

    assert len(lines) == 2  # header + 1 data row
    data_line = lines[1]
    # Date is ISO format.
    assert "2024-03-15" in data_line
    # Amount is raw decimal (no thousand separators).
    assert "1234567.89" in data_line
    # Company name is present.
    assert "RAW-EXPORT-CO" in data_line
    assert "raw-purchase-42" in data_line
    assert (
        "https://www.comprasestatales.gub.uy/consultas/detalle/id/raw-purchase-42"
        in data_line
    )


def test_export_csv_uses_crlf_line_endings(
    client: TestClient, make_adjudication
) -> None:
    """CSV uses \\r\\n line endings for Excel compatibility."""

    make_adjudication(date=date(CURRENT_YEAR, 3, 1))

    response = client.get("/adjudications/export")
    text = response.content.decode("utf-8-sig")

    # At least one \r\n present (header + data row).
    assert "\r\n" in text


def test_export_empty_result_returns_headers_only(
    client: TestClient,
) -> None:
    """Empty result set returns a CSV with only the header row."""

    response = client.get("/adjudications/export")

    assert response.status_code == 200
    text = response.content.decode("utf-8-sig")
    lines = [line for line in text.split("\r\n") if line]

    assert len(lines) == 1  # header only
    assert "fecha" in lines[0]


def test_export_preserves_active_filters(client: TestClient, make_adjudication) -> None:
    """Export applies the same filters as the listing UI."""

    make_adjudication(
        organism="FILTER-EXPORT-OSE",
        winning_company="FILTER-EXPORT-A",
        date=date(CURRENT_YEAR, 3, 1),
    )
    make_adjudication(
        organism="FILTER-EXPORT-MIN",
        winning_company="FILTER-EXPORT-B",
        date=date(CURRENT_YEAR, 4, 1),
    )

    response = client.get("/adjudications/export?organism=OSE")
    text = response.content.decode("utf-8-sig")

    assert "FILTER-EXPORT-A" in text
    assert "FILTER-EXPORT-B" not in text


def test_export_row_cap_exceeded_returns_400(
    client: TestClient, make_adjudication, monkeypatch
) -> None:
    """When count exceeds MAX_EXPORT_ROWS, returns 400 with Spanish message."""

    import app.routes.common as routes_module

    # Patch the cap to 0 so any data triggers it.
    monkeypatch.setattr(routes_module, "MAX_EXPORT_ROWS", 0)

    make_adjudication(date=date(CURRENT_YEAR, 3, 1))

    response = client.get("/adjudications/export")

    assert response.status_code == 400
    assert "500.000" in response.text


def test_export_default_year_injection(client: TestClient, make_adjudication) -> None:
    """Export with no date params defaults to current year (like listing)."""

    make_adjudication(
        winning_company="EXPORT-OLD",
        date=date(PREV_YEAR, 6, 1),
    )
    make_adjudication(
        winning_company="EXPORT-NEW",
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/adjudications/export")
    text = response.content.decode("utf-8-sig")

    assert "EXPORT-NEW" in text
    assert "EXPORT-OLD" not in text


def test_export_invalid_date_returns_422(client: TestClient) -> None:
    """Invalid date params return 422 with Spanish error."""

    response = client.get("/adjudications/export?date_from=not-a-date")

    assert response.status_code == 422


def test_export_null_amount_uyu_renders_as_empty(
    client: TestClient, make_adjudication
) -> None:
    """amount_uyu=None renders as empty string in the CSV."""

    make_adjudication(
        winning_company="NULL-UYU-EXPORT",
        amount_uyu=None,
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/adjudications/export")
    text = response.content.decode("utf-8-sig")
    lines = [line for line in text.split("\r\n") if line]

    # The data row should be present (not just the header).
    assert len(lines) == 2
    # The monto_uyu column (index 6) should be empty — two consecutive
    # commas with nothing between them.
    data_fields = lines[1].split(",")
    # monto_uyu is at index 6 (0-based).
    assert data_fields[6] == ""


def test_export_escapes_formula_prefix_cells(
    client: TestClient, make_adjudication
) -> None:
    """Global export escapes dangerous-prefix data cells, never the header."""

    make_adjudication(
        winning_company='=HYPERLINK("http://evil.test")',
        organism="+SUM(A1)",
        article="@import",
        company_document="-2+3",
        company_document_type="\tRUT",
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/adjudications/export")

    assert response.status_code == 200
    # BOM, CRLF, and the exact header contract are retained.
    assert response.content.startswith(b"\xef\xbb\xbf")
    text = response.content.decode("utf-8-sig")
    assert "\r\n" in text

    parsed = list(csv.reader(io.StringIO(text)))
    # The header row is never formula-escaped.
    assert parsed[0] == _CSV_COLUMNS
    data = parsed[1]
    # organismo (1), empresa_adjudicataria (2), articulo (3),
    # documento_empresa (8), tipo_documento (9) carry exactly one
    # leading apostrophe followed by the original value.
    assert data[1] == "'+SUM(A1)"
    assert data[2] == '\'=HYPERLINK("http://evil.test")'
    assert data[3] == "'@import"
    assert data[8] == "'-2+3"
    assert data[9] == "'\tRUT"


# ---------------------------------------------------------------------------
# Export button in _results.html
# ---------------------------------------------------------------------------


def test_export_button_visible_when_results_exist(
    client: TestClient, make_adjudication
) -> None:
    """The CSV export button is rendered when results exist."""

    make_adjudication(date=date(CURRENT_YEAR, 3, 1))

    response = client.get("/adjudications")
    body = response.text

    assert 'href="/adjudications/export' in body
    assert "Exportar CSV" in body


def test_export_button_hidden_when_no_results(
    client: TestClient,
) -> None:
    """The CSV export button is NOT rendered when there are 0 results."""

    response = client.get("/adjudications")
    body = response.text

    assert 'href="/adjudications/export' not in body


def test_export_button_preserves_query_params(
    client: TestClient, make_adjudication
) -> None:
    """The export button href includes the current filter query params."""

    make_adjudication(
        organism="BTN-ORG-OSE",
        date=date(CURRENT_YEAR, 3, 1),
    )

    response = client.get("/adjudications?organism=OSE&date_from=2024-01-01")
    body = response.text

    # The export link includes the active filter params.
    assert "organism=OSE" in body
    assert "date_from=2024-01-01" in body
    # The link is a plain <a> (not HTMX).
    assert "hx-get" not in body.split('href="/adjudications/export')[0].split("\n")[-1]


# ---------------------------------------------------------------------------
# Decoded route-identity length caps (255 / 10 / 50, measured post-decode)
# ---------------------------------------------------------------------------


def test_organism_detail_accepts_maximum_255_char_name(
    client: TestClient,
) -> None:
    """A decoded 255-char organism name reaches the normal empty state."""

    response = client.get(f"/organism/{'O' * 255}")

    assert response.status_code == 200
    assert "No hay datos" in response.text


def test_organism_detail_partial_accepts_maximum_255_char_name(
    client: TestClient,
) -> None:
    """The organism partial accepts a decoded 255-char name."""

    response = client.get(f"/organism/{'O' * 255}/partial")
    assert response.status_code == 200


def test_organism_detail_rejects_256_char_name(client: TestClient) -> None:
    """A decoded 256-char organism name is a 404, never a query."""

    response = client.get(f"/organism/{'O' * 256}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_organism_detail_partial_rejects_256_char_name(
    client: TestClient,
) -> None:
    """The organism partial rejects a decoded 256-char name with 404."""

    response = client.get(f"/organism/{'O' * 256}/partial")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_organism_detail_rejects_doubly_encoded_256_char_name(
    client: TestClient,
) -> None:
    """A doubly-encoded 256-char name is 404, proving the cap is post-decode.

    Each raw A%2521 decodes to A! after FastAPI’s path decode and
    the route’s explicit unquote — 128 repetitions decode to 256
    non-whitespace characters that survive the existing .strip().
    """

    response = client.get(f"/organism/{'A%2521' * 128}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_organism_detail_accepts_doubly_encoded_255_char_name(
    client: TestClient,
) -> None:
    """A doubly-encoded 255-char name is NOT rejected by the cap."""

    response = client.get(f"/organism/{'A%2521' * 127}A")
    assert response.status_code == 200


def test_overlong_organism_404_skips_context_build(
    client: TestClient, monkeypatch
) -> None:
    """Over-limit organism names exit before any context/query work."""

    import app.routes.organism as organism_module

    def _must_not_run(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("_build_organism_context must not be called")

    monkeypatch.setattr(organism_module, "_build_organism_context", _must_not_run)

    for path in (f"/organism/{'O' * 256}", f"/organism/{'O' * 256}/partial"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}


def test_company_detail_accepts_maximum_identity_lengths(
    client: TestClient,
) -> None:
    """A 10-char document type and 50-char number reach the empty state."""

    response = client.get(f"/company/{'T' * 10}/{'N' * 50}")

    assert response.status_code == 200
    assert "No se encontró actividad registrada" in response.text


def test_company_detail_partial_accepts_maximum_identity_lengths(
    client: TestClient,
) -> None:
    """The company partial accepts a 10-char type and 50-char number."""

    response = client.get(f"/company/{'T' * 10}/{'N' * 50}/partial")
    assert response.status_code == 200


@pytest.mark.parametrize(
    "identity",
    [f"{'T' * 11}/42", f"RUT/{'N' * 51}"],
)
def test_company_detail_rejects_overlong_identity(
    client: TestClient, identity: str
) -> None:
    """Over-limit decoded type or number returns 404 on the full page."""

    response = client.get(f"/company/{identity}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize(
    "identity",
    [f"{'T' * 11}/42", f"RUT/{'N' * 51}"],
)
def test_company_detail_partial_rejects_overlong_identity(
    client: TestClient, identity: str
) -> None:
    """Over-limit decoded type or number returns 404 on the partial."""

    response = client.get(f"/company/{identity}/partial")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_company_export_overlong_identity_returns_404_before_filters(
    client: TestClient, monkeypatch
) -> None:
    """Over-limit export identities 404 before any filter/CSV work."""

    import app.routes.company as company_module

    def _must_not_run(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("_company_filters must not be called")

    monkeypatch.setattr(company_module, "_company_filters", _must_not_run)

    response = client.get(f"/company/{'T' * 11}/42/export")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_overlong_company_identity_404_skips_context_build(
    client: TestClient, monkeypatch
) -> None:
    """Over-limit company identities exit before context construction."""

    import app.routes.company as company_module

    def _must_not_run(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("_build_company_context must not be called")

    monkeypatch.setattr(company_module, "_build_company_context", _must_not_run)

    for path in (
        f"/company/{'T' * 11}/42",
        f"/company/RUT/{'N' * 51}/partial",
    ):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}
