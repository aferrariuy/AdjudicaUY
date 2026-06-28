"""Unit tests for the citizen-dashboard aggregate queries.

The functions tested here (:func:`kpi_summary`, :func:`monthly_trend`,
:func:`concentration_ratio`) live in :mod:`app.services.adjudication_service`
and own the SQLAlchemy-side of the dashboard. They are exercised here
against the in-memory SQLite engine from ``conftest.py`` — no HTTP
layer, no network, no chart payloads. The route-side coverage of
the new widgets (HTML rendering, JSON payload shape, organism links)
lives in :mod:`tests.app.test_routes`.

Each test seeds a small fixture directly via the ORM (rather than
through the ``make_adjudication`` factory) so the assertions can
target a known composition of compras / adjudicaciones / oferentes
without the factory's per-call id increment getting in the way.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from app.models.oferente import Oferente
from app.services.adjudication_service import (
    AdjudicationFilters,
    ConcentrationResult,
    KpiSummary,
    ValidationError,
    concentration_ratio,
    kpi_summary,
    list_adjudications,
    monthly_trend,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Local fixtures — keep these next to the tests that need them so the
# conftest stays small. Each fixture returns a builder that composes
# compras + adjudicaciones + oferentes in a known shape.
# ---------------------------------------------------------------------------


@pytest.fixture
def make_compra(db_session: Session):
    """Factory: persist a Compra with explicit id_compra / date / organism.

    The default fecha_pub_adj lands in the current year so the route's
    default-year injection does not hide the row when the test goes
    through the HTTP layer (it does not here, but the discipline is
    cheap and keeps the seed data uniform).
    """

    counter = {"n": 0}

    def _factory(
        *,
        id_compra: str | None = None,
        fecha_pub_adj: date | None = None,
        organismo: str | None = None,
    ) -> Compra:
        counter["n"] += 1
        compra = Compra(
            id_compra=id_compra or f"compra-{counter['n']}",
            fecha_pub_adj=fecha_pub_adj or date(date.today().year, 1, 15),
            id_tipocompra="CD",
            organismo=organismo,
        )
        db_session.add(compra)
        db_session.flush()
        return compra

    return _factory


@pytest.fixture
def add_adj(db_session: Session):
    """Factory: attach an Adjudicacion to an existing Compra.

    The default values produce a non-null ``amount_uyu`` and a
    distinct ``nombre_comercial`` per call (named after the
    counter) so COUNT(DISTINCT) assertions are easy to read.
    """

    counter = {"n": 0}

    def _factory(
        compra: Compra,
        *,
        nombre_comercial: str | None = None,
        desc_articulo: str = "Laptop",
        id_articulo: str | None = None,
        amount_uyu: Decimal | None = Decimal("1000.00"),
        precio_tot_imp: Decimal = Decimal("1000.00"),
    ) -> Adjudicacion:
        counter["n"] += 1
        adj = Adjudicacion(
            compra_id=compra.id,
            nombre_comercial=nombre_comercial or f"Empresa {counter['n']}",
            desc_articulo=desc_articulo,
            id_articulo=id_articulo,
            id_moneda=0,
            precio_tot_imp=precio_tot_imp,
            amount_uyu=amount_uyu,
        )
        db_session.add(adj)
        db_session.flush()
        return adj

    return _factory


@pytest.fixture
def add_oferente(db_session: Session):
    """Factory: attach an Oferente to an existing Compra."""

    counter = {"n": 0}

    def _factory(compra: Compra, *, nombre_comercial: str | None = None) -> Oferente:
        counter["n"] += 1
        of = Oferente(
            compra_id=compra.id,
            nombre_comercial=nombre_comercial or f"Bidder {counter['n']}",
        )
        db_session.add(of)
        db_session.flush()
        return of

    return _factory


# ---------------------------------------------------------------------------
# kpi_summary
# ---------------------------------------------------------------------------


class TestKpiSummary:
    """Aggregate metrics for the filtered adjudication set.

    Mirrors the kpi-summary spec ("KPI Card Set" + "KPI Empty State"
    + "KPI Number Formatting" requirements). The service returns
    raw ``Decimal`` / ``int`` values — number formatting is a
    template concern, not a service concern.
    """

    def test_multi_row_totals_sum_amounts_and_count_distinct(
        self, db_session, make_compra, add_adj
    ) -> None:
        c1 = make_compra()
        c2 = make_compra()
        # Two compras, each with two adjudicaciones, distinct winners.
        add_adj(c1, nombre_comercial="Acme", amount_uyu=Decimal("1000.00"))
        add_adj(c1, nombre_comercial="Globex", amount_uyu=Decimal("500.00"))
        add_adj(c2, nombre_comercial="Initech", amount_uyu=Decimal("200.00"))
        add_adj(c2, nombre_comercial="Hooli", amount_uyu=Decimal("100.00"))

        result = kpi_summary(db_session, AdjudicationFilters())

        assert isinstance(result, KpiSummary)
        # SUM of the four amounts.
        assert result.total_amount == Decimal("1800.00")
        # 1800 / 4 non-null rows = 450.
        assert result.average_amount == Decimal("450.00")
        # 2 distinct compras (the DISTINCT on Compra.id dedupes the
        # two adjudicaciones per compra).
        assert result.purchase_count == 2
        # 4 distinct winning companies.
        assert result.company_count == 4

    def test_null_amount_uyu_excluded_from_sum_and_average(
        self, db_session, make_compra, add_adj
    ) -> None:
        c1 = make_compra()
        add_adj(c1, nombre_comercial="Convertible", amount_uyu=Decimal("1000.00"))
        # Non-convertible currencies store NULL on amount_uyu.
        add_adj(
            c1,
            nombre_comercial="NonConvertible",
            amount_uyu=None,
        )

        result = kpi_summary(db_session, AdjudicationFilters())

        # NULL amount is excluded from the SUM.
        assert result.total_amount == Decimal("1000.00")
        # And from the COUNT used for the average, so 1000 / 1 = 1000.
        assert result.average_amount == Decimal("1000.00")
        # But the company is still distinct (we counted by DISTINCT
        # nombre_comercial, not by amount).
        assert result.company_count == 2

    def test_count_distinct_companies_dedupes_repeated_winners(
        self, db_session, make_compra, add_adj
    ) -> None:
        c1 = make_compra()
        c2 = make_compra()
        # Same winning company on two compras — must count once.
        add_adj(c1, nombre_comercial="Acme", amount_uyu=Decimal("100.00"))
        add_adj(c2, nombre_comercial="Acme", amount_uyu=Decimal("200.00"))
        add_adj(c2, nombre_comercial="Globex", amount_uyu=Decimal("300.00"))

        result = kpi_summary(db_session, AdjudicationFilters())

        assert result.company_count == 2  # Acme + Globex
        # Total still sums every row.
        assert result.total_amount == Decimal("600.00")
        # Purchase count is the number of distinct compras.
        assert result.purchase_count == 2

    def test_empty_set_returns_zero_values(self, db_session, make_compra) -> None:
        # Compra exists but with no adjudicaciones — the join produces
        # zero rows, so all aggregates should be 0.
        make_compra()

        result = kpi_summary(db_session, AdjudicationFilters())

        assert result.total_amount == Decimal(0)
        # Average is also 0 in the empty case (avoids div-by-zero).
        assert result.average_amount == Decimal(0)
        assert result.purchase_count == 0
        assert result.company_count == 0

    def test_empty_db_returns_zero_values(self, db_session) -> None:
        result = kpi_summary(db_session, AdjudicationFilters())

        assert result.total_amount == Decimal(0)
        assert result.average_amount == Decimal(0)
        assert result.purchase_count == 0
        assert result.company_count == 0

    def test_filter_scope_excludes_out_of_set_rows(
        self, db_session, make_compra, add_adj
    ) -> None:
        c1 = make_compra(organismo="OSE")
        c2 = make_compra(organismo="Ministerio de Interior")
        add_adj(c1, nombre_comercial="A", amount_uyu=Decimal("1000.00"))
        add_adj(c2, nombre_comercial="B", amount_uyu=Decimal("500.00"))

        # ILIKE partial match on organism mirrors the existing route.
        result = kpi_summary(db_session, AdjudicationFilters(organism="OSE"))

        assert result.total_amount == Decimal("1000.00")
        assert result.purchase_count == 1
        assert result.company_count == 1


# ---------------------------------------------------------------------------
# monthly_trend
# ---------------------------------------------------------------------------


class TestMonthlyTrend:
    """Monthly grouping + sparse-month fill behaviour.

    Mirrors the temporal-trend spec ("Monthly Trend Chart" requirement)
    — the function returns ``[(YYYY-MM, total), ...]`` in chronological
    order, with sparse months zero-filled within the active window.
    """

    def test_multi_month_returns_one_entry_per_month(
        self, db_session, make_compra, add_adj
    ) -> None:
        c1 = make_compra(fecha_pub_adj=date(2024, 1, 5))
        c2 = make_compra(fecha_pub_adj=date(2024, 2, 10))
        c3 = make_compra(fecha_pub_adj=date(2024, 3, 20))
        add_adj(c1, amount_uyu=Decimal("1000.00"))
        add_adj(c2, amount_uyu=Decimal("2000.00"))
        add_adj(c3, amount_uyu=Decimal("3000.00"))

        result = monthly_trend(db_session, AdjudicationFilters())

        assert result == [
            ("2024-01", Decimal("1000.00")),
            ("2024-02", Decimal("2000.00")),
            ("2024-03", Decimal("3000.00")),
        ]

    def test_single_month_returns_one_entry(
        self, db_session, make_compra, add_adj
    ) -> None:
        c1 = make_compra(fecha_pub_adj=date(2024, 5, 1))
        c2 = make_compra(fecha_pub_adj=date(2024, 5, 15))
        add_adj(c1, amount_uyu=Decimal("100.00"))
        add_adj(c2, amount_uyu=Decimal("200.00"))

        result = monthly_trend(db_session, AdjudicationFilters())

        # Both rows collapse into the single month.
        assert result == [("2024-05", Decimal("300.00"))]

    def test_sparse_months_are_filled_with_zero(
        self, db_session, make_compra, add_adj
    ) -> None:
        c1 = make_compra(fecha_pub_adj=date(2024, 1, 10))
        c3 = make_compra(fecha_pub_adj=date(2024, 3, 10))
        add_adj(c1, amount_uyu=Decimal("1000.00"))
        add_adj(c3, amount_uyu=Decimal("500.00"))

        result = monthly_trend(db_session, AdjudicationFilters())

        # February is filled with 0 so the chart has no gap.
        assert result == [
            ("2024-01", Decimal("1000.00")),
            ("2024-02", Decimal("0")),
            ("2024-03", Decimal("500.00")),
        ]

    def test_date_filter_widens_window_to_include_zero_months(
        self, db_session, make_compra, add_adj
    ) -> None:
        c1 = make_compra(fecha_pub_adj=date(2024, 1, 10))
        add_adj(c1, amount_uyu=Decimal("1000.00"))

        c2 = make_compra(fecha_pub_adj=date(2024, 3, 15))
        add_adj(c2, amount_uyu=Decimal("500.00"))

        # Active window: Jan 1 → Jun 30 — months with data are
        # included (Jan, Mar) and the gap (Feb) is filled with zero.
        # Months AFTER the last data point (Apr–Jun) are NOT included
        # so the trend line doesn't extend into empty future months.
        result = monthly_trend(
            db_session,
            AdjudicationFilters(date_from=date(2024, 1, 1), date_to=date(2024, 6, 30)),
        )

        assert [label for label, _total in result] == [
            "2024-01",
            "2024-02",
            "2024-03",
        ]
        assert result[0] == ("2024-01", Decimal("1000.00"))
        assert result[1] == ("2024-02", Decimal("0"))
        assert result[2] == ("2024-03", Decimal("500.00"))

    def test_empty_set_returns_empty_list(self, db_session) -> None:
        assert monthly_trend(db_session, AdjudicationFilters()) == []

    def test_null_amounts_excluded_yields_empty_set(
        self, db_session, make_compra, add_adj
    ) -> None:
        # A compra with adjudicaciones whose amount_uyu is all NULL
        # does not contribute to any group — same observable behaviour
        # as the empty set (the spec's "Data exists but all amounts
        # are null" empty-state scenario).
        c1 = make_compra(fecha_pub_adj=date(2024, 1, 10))
        add_adj(c1, amount_uyu=None)

        assert monthly_trend(db_session, AdjudicationFilters()) == []

    def test_filter_scope_respects_organism(
        self, db_session, make_compra, add_adj
    ) -> None:
        c1 = make_compra(organismo="OSE", fecha_pub_adj=date(2024, 1, 10))
        c2 = make_compra(
            organismo="Ministerio de Interior", fecha_pub_adj=date(2024, 1, 20)
        )
        add_adj(c1, amount_uyu=Decimal("1000.00"))
        add_adj(c2, amount_uyu=Decimal("500.00"))

        result = monthly_trend(db_session, AdjudicationFilters(organism="OSE"))

        assert result == [("2024-01", Decimal("1000.00"))]


# ---------------------------------------------------------------------------
# concentration_ratio
# ---------------------------------------------------------------------------


class TestConcentrationRatio:
    """Single-bidder / multi-bidder share of the filtered compras.

    Mirrors the market-concentration spec ("Concentration Metric"
    requirement). A Compra with 0 oferentes is excluded from BOTH
    numerator and denominator — the metric is undefined for it.
    """

    def test_all_single_bidder_returns_full_ratio(
        self, db_session, make_compra, add_adj, add_oferente
    ) -> None:
        c1 = make_compra()
        c2 = make_compra()
        add_adj(c1, amount_uyu=Decimal("1000.00"))
        add_adj(c2, amount_uyu=Decimal("500.00"))
        add_oferente(c1)
        add_oferente(c2)

        result = concentration_ratio(db_session, AdjudicationFilters())

        assert isinstance(result, ConcentrationResult)
        assert result.single_bidder_count == 2
        assert result.multi_bidder_count == 0
        assert result.ratio == Decimal("1")

    def test_all_multi_bidder_returns_zero_ratio(
        self, db_session, make_compra, add_adj, add_oferente
    ) -> None:
        c1 = make_compra()
        c2 = make_compra()
        add_adj(c1, amount_uyu=Decimal("1000.00"))
        add_adj(c2, amount_uyu=Decimal("500.00"))
        # Two oferentes per compra.
        add_oferente(c1)
        add_oferente(c1, nombre_comercial="B2")
        add_oferente(c2)
        add_oferente(c2, nombre_comercial="B3")

        result = concentration_ratio(db_session, AdjudicationFilters())

        assert result.single_bidder_count == 0
        assert result.multi_bidder_count == 2
        assert result.ratio == Decimal("0")

    def test_compras_with_zero_oferentes_excluded_returns_none_ratio(
        self, db_session, make_compra, add_adj
    ) -> None:
        # Compra with an adjudicated line but no oferentes (the
        # 0-oferente exclusion scenario from the spec). Ratio must
        # be None because the denominator is zero.
        c1 = make_compra()
        add_adj(c1, amount_uyu=Decimal("1000.00"))
        # No add_oferente call.

        result = concentration_ratio(db_session, AdjudicationFilters())

        assert result.single_bidder_count == 0
        assert result.multi_bidder_count == 0
        assert result.ratio is None

    def test_mixed_set_buckets_each_compra(
        self, db_session, make_compra, add_adj, add_oferente
    ) -> None:
        # 1 single-bidder compra, 1 multi-bidder compra, 1 zero-oferente
        # compra. The zero-oferente one is excluded from both
        # numerator and denominator, so the ratio is 1 / 2.
        c_single = make_compra()
        c_multi = make_compra()
        c_none = make_compra()
        add_adj(c_single, amount_uyu=Decimal("1000.00"))
        add_adj(c_multi, amount_uyu=Decimal("500.00"))
        add_adj(c_none, amount_uyu=Decimal("200.00"))
        add_oferente(c_single)
        add_oferente(c_multi)
        add_oferente(c_multi, nombre_comercial="Second bidder")
        # c_none: no oferentes

        result = concentration_ratio(db_session, AdjudicationFilters())

        assert result.single_bidder_count == 1
        assert result.multi_bidder_count == 1
        assert result.ratio == Decimal("0.5")

    def test_filter_scope_respects_organism(
        self, db_session, make_compra, add_adj, add_oferente
    ) -> None:
        c1 = make_compra(organismo="OSE")
        c2 = make_compra(organismo="Ministerio de Interior")
        add_adj(c1, amount_uyu=Decimal("1000.00"))
        add_adj(c2, amount_uyu=Decimal("500.00"))
        add_oferente(c1)
        add_oferente(c2)

        # Only the OSE compra is in scope; the other is filtered out.
        result = concentration_ratio(db_session, AdjudicationFilters(organism="OSE"))

        assert result.single_bidder_count == 1
        assert result.multi_bidder_count == 0
        assert result.ratio == Decimal("1")

    def test_empty_db_returns_none_ratio(self, db_session) -> None:
        result = concentration_ratio(db_session, AdjudicationFilters())
        assert result.single_bidder_count == 0
        assert result.multi_bidder_count == 0
        assert result.ratio is None

    def test_compra_with_multiple_adjudicaciones_counted_once(
        self, db_session, make_compra, add_adj, add_oferente
    ) -> None:
        """The unit of analysis is the Compra, not the Adjudicacion.

        A Compra can have multiple line items (one Adjudicacion per
        article) but it has a single set of oferentes. We must
        deduplicate the Compra before bucketing — otherwise the
        correlated subquery would over-count the oferentes.
        """

        c1 = make_compra()
        # Three adjudicaciones on the same compra, but only one oferente.
        add_adj(c1, nombre_comercial="A", amount_uyu=Decimal("100.00"))
        add_adj(c1, nombre_comercial="B", amount_uyu=Decimal("200.00"))
        add_adj(c1, nombre_comercial="C", amount_uyu=Decimal("300.00"))
        add_oferente(c1)

        result = concentration_ratio(db_session, AdjudicationFilters())

        # The compra is single-bidder; multi-bidder is 0; ratio 1.
        assert result.single_bidder_count == 1
        assert result.multi_bidder_count == 0
        assert result.ratio == Decimal("1")


# ---------------------------------------------------------------------------
# Filter hardening
# ---------------------------------------------------------------------------


def test_article_id_filter_capped_at_max_values(
    db_session, make_compra, add_adj
) -> None:
    """An ``article_id`` list above the cap is rejected instead of building a
    huge SQL ``IN`` clause."""

    c = make_compra()
    add_adj(c, id_articulo="1")
    ids = ",".join(str(i) for i in range(250))
    with pytest.raises(ValidationError, match="más de"):
        list_adjudications(
            db_session,
            AdjudicationFilters(article_id=ids),
        )


def test_like_wildcards_are_escaped_in_text_filters(
    db_session, make_compra, add_adj
) -> None:
    """``%`` and ``_`` in user input are treated as literal characters, not
    SQL wildcards."""

    c = make_compra()
    add_adj(c, nombre_comercial="Empresa_SA", desc_articulo="Laptop%20")

    # Underscore should not match any single character.
    rows = list_adjudications(
        db_session,
        AdjudicationFilters(company="Empresa_SA"),
    )
    assert len(rows) == 1

    # Percent should not match everything.
    rows = list_adjudications(
        db_session,
        AdjudicationFilters(article="Laptop%20"),
    )
    assert len(rows) == 1

