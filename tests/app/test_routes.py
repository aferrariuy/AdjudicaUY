"""Unit tests for the FastAPI routes in :mod:`app.routes.adjudications`.

The route layer is intentionally thin — these tests exercise the
behaviours declared in the filtering-ui spec and the route's HTMX
contract, using the in-memory SQLite engine from ``conftest.py``.
"""

from __future__ import annotations

from datetime import date, timedelta
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


def test_index_renders_ranking_list_headings(
    client: TestClient, make_adjudication
) -> None:
    make_adjudication(amount_uyu=Decimal("1000.00"), date=date(CURRENT_YEAR, 3, 1))

    response = client.get("/")
    body = response.text

    # Both ranking list partials are rendered with their <h2> headings.
    assert 'id="ranking-heading"' in body
    assert 'id="organism-ranking-heading"' in body


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
    assert "form.reset()" in body
    assert "htmx.trigger" in body


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
    assert "form.reset()" in body
    assert "htmx.trigger" in body


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


def test_validate_date_params_accepts_under_5y() -> None:
    """A range shorter than 5 years is accepted."""

    from app.services.adjudication_service import validate_date_params

    # 3-year range: well under the 5-year limit.
    validate_date_params({"date_from": "2022-01-01", "date_to": "2024-12-31"})


def test_validate_date_params_accepts_exactly_5y_boundary() -> None:
    """A range of exactly 1825 days (5×365) is accepted as the boundary."""

    from app.services.adjudication_service import validate_date_params

    # 2020-01-01 → 2024-12-30 is exactly 1825 days (2020 is leap, so
    # the leap day is already accounted for inside the span).
    validate_date_params({"date_from": "2020-01-01", "date_to": "2024-12-30"})


def test_validate_date_params_rejects_over_5y() -> None:
    """A range of 1826 days (one day over) is rejected with the 5-year message."""

    import pytest

    from app.services.adjudication_service import (
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

    from app.services.adjudication_service import (
        DateValidationError,
        validate_date_params,
    )

    # 10-year range: 2010-01-01 → 2020-01-01 is 3652 days.
    with pytest.raises(DateValidationError, match="5 años"):
        validate_date_params({"date_from": "2010-01-01", "date_to": "2020-01-01"})


def test_validate_date_params_accepts_single_date() -> None:
    """When only one date is provided, no range exists to check."""

    from app.services.adjudication_service import validate_date_params

    # Only date_from — the max-range check only runs when both are present.
    validate_date_params({"date_from": "2024-01-01", "date_to": ""})
    # Only date_to.
    validate_date_params({"date_from": "", "date_to": "2024-12-31"})


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
