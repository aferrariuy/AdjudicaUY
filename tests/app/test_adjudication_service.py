"""Unit tests for the citizen-dashboard aggregate queries.

The functions tested here (:func:`kpi_summary`, :func:`monthly_trend`,
:func:`concentration_ratio`) live in :mod:`app.services.dashboard`
and related service modules. They are exercised here
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
from typing import TYPE_CHECKING, cast

import pytest
import sqlalchemy as sa

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from app.models.oferente import Oferente
from app.services.company import (
    CompanyProfileSummary,
    CompanyWinRate,
    _document_pair_match,
    _scoped_compra_ids,
    company_competitors,
    company_summary,
    company_win_rate,
    lookup_company_identities,
    lookup_company_identity,
)
from app.services.dashboard import (
    ConcentrationResult,
    KpiSummary,
    concentration_ratio,
    kpi_summary,
    monthly_trend,
    ranking_by_company,
    ranking_by_organism,
    top_articles,
)
from app.services.filters import AdjudicationFilters, ValidationError
from app.services.listing import count_adjudications, list_adjudications

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
        tipo_doc_prov: str | None = "RUT",
        nro_doc_prov: str | None = None,
        desc_articulo: str = "Laptop",
        id_articulo: str | None = None,
        amount_uyu: Decimal | None = Decimal("1000.00"),
        precio_tot_imp: Decimal = Decimal("1000.00"),
    ) -> Adjudicacion:
        counter["n"] += 1
        adj = Adjudicacion(
            compra_id=compra.id,
            nombre_comercial=nombre_comercial or f"Empresa {counter['n']}",
            tipo_doc_prov=tipo_doc_prov,
            nro_doc_prov=nro_doc_prov or f"2100000000{counter['n']:02d}",
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


def _legacy_company_competitor_tuples(
    session: Session,
    company_type: str,
    company_number: str,
    filters: AdjudicationFilters,
    *,
    limit: int = 5,
) -> list[tuple[str, str, int, Decimal, str]]:
    scoped_ids = _scoped_compra_ids(session, filters).subquery("company_scope")
    target_ids = (
        sa.select(Oferente.compra_id)
        .where(
            Oferente.compra_id.in_(sa.select(scoped_ids.c.id)),
            _document_pair_match(Oferente, company_type, company_number),
        )
        .distinct()
        .subquery("target_purchases")
    )
    valid_competitor = sa.and_(
        Oferente.tipo_doc_prov.is_not(None),
        Oferente.nro_doc_prov.is_not(None),
        ~sa.and_(
            Oferente.tipo_doc_prov == company_type,
            Oferente.nro_doc_prov == company_number,
        ),
    )
    award_total = (
        sa.select(sa.func.coalesce(sa.func.sum(Adjudicacion.amount_uyu), 0))
        .where(
            Adjudicacion.compra_id.in_(sa.select(target_ids.c.compra_id)),
            Adjudicacion.tipo_doc_prov == Oferente.tipo_doc_prov,
            Adjudicacion.nro_doc_prov == Oferente.nro_doc_prov,
        )
        .correlate(Oferente)
        .scalar_subquery()
    )
    counts = (
        sa.select(
            Oferente.tipo_doc_prov.label("company_type"),
            Oferente.nro_doc_prov.label("company_number"),
            sa.func.max(Oferente.nombre_comercial).label("fallback_name"),
            sa.func.count(sa.func.distinct(Oferente.compra_id)).label("purchase_count"),
            award_total.label("awarded_amount_uyu"),
        )
        .where(
            Oferente.compra_id.in_(sa.select(target_ids.c.compra_id)), valid_competitor
        )
        .group_by(Oferente.tipo_doc_prov, Oferente.nro_doc_prov)
        .subquery("competitor_counts")
    )
    rows = list(session.execute(sa.select(counts)))
    pairs = [(row.company_type, row.company_number) for row in rows]
    identities = lookup_company_identities(session, pairs)
    competitors = [
        (
            row.company_type,
            row.company_number,
            int(row.purchase_count),
            Decimal(row.awarded_amount_uyu or 0),
            identities.get((row.company_type, row.company_number))
            or row.fallback_name
            or "Empresa sin nombre",
        )
        for row in rows
    ]
    competitors.sort(key=lambda row: (-row[2], -row[3], row[4]))
    return competitors[: max(limit, 0)]


def _seed_company_competitor_edges(
    make_compra, add_adj, make_oferente
) -> tuple[str, str]:
    target = ("RUT", "TARGET")

    def bid(compra: Compra, pair: tuple[str | None, str | None], name: str) -> None:
        make_oferente(
            compra.id,
            nombre_comercial=name,
            tipo_doc_prov=pair[0],
            nro_doc_prov=pair[1],
        )

    first, second, third = (
        make_compra(fecha_pub_adj=date(2024, 1, day)) for day in (1, 2, 3)
    )
    for compra in (first, second, third):
        bid(compra, target, "Target bidder")

    bid(first, ("RUT", "COMP-A"), "Fallback A first")
    bid(first, ("RUT", "COMP-A"), "Fallback A duplicate")
    bid(second, ("RUT", "COMP-A"), "Fallback A second")
    bid(first, ("RUT", "COMP-B"), "Fallback B first")
    bid(third, ("RUT", "COMP-B"), "Fallback B third")
    bid(first, ("RUT", "COMP-F"), "Fallback F")
    bid(first, (None, "NULL-TYPE"), "Null type")
    bid(first, ("RUT", None), "Null number")
    bid(first, (None, None), "Absent pair")

    add_adj(
        first,
        nombre_comercial="Canonical A",
        tipo_doc_prov="RUT",
        nro_doc_prov="COMP-A",
        desc_articulo="A line one",
        amount_uyu=Decimal("100.00"),
    )
    add_adj(
        first,
        nombre_comercial="Canonical A",
        tipo_doc_prov="RUT",
        nro_doc_prov="COMP-A",
        desc_articulo="A line two",
        amount_uyu=Decimal("50.00"),
    )
    add_adj(
        second,
        nombre_comercial="Canonical A",
        tipo_doc_prov="RUT",
        nro_doc_prov="COMP-A",
        desc_articulo="A award without bid",
        amount_uyu=Decimal("25.00"),
    )
    add_adj(
        first,
        nombre_comercial="Canonical B",
        tipo_doc_prov="RUT",
        nro_doc_prov="COMP-B",
        desc_articulo="B line",
        amount_uyu=Decimal("80.00"),
    )
    add_adj(
        third,
        nombre_comercial="Canonical B",
        tipo_doc_prov="RUT",
        nro_doc_prov="COMP-B",
        desc_articulo="B null line",
        amount_uyu=None,
    )
    add_adj(
        first,
        nombre_comercial="Target winner",
        tipo_doc_prov=target[0],
        nro_doc_prov=target[1],
        desc_articulo="Target line",
        amount_uyu=Decimal("999.00"),
    )
    add_adj(
        first,
        nombre_comercial="Award-only",
        tipo_doc_prov="RUT",
        nro_doc_prov="AWARD-ONLY",
        desc_articulo="No bid line",
        amount_uyu=Decimal("700.00"),
    )

    outsider = make_compra(fecha_pub_adj=date(2023, 1, 1))
    bid(outsider, ("RUT", "OUTSIDER"), "Outsider")
    add_adj(
        outsider,
        nombre_comercial="Outsider",
        tipo_doc_prov="RUT",
        nro_doc_prov="OUTSIDER",
        desc_articulo="Outsider line",
        amount_uyu=Decimal("600.00"),
    )
    return target


def _legacy_company_win_rate_tuples(
    session: Session,
    company_type: str,
    company_number: str,
    filters: AdjudicationFilters,
) -> tuple[int, int, Decimal | None]:
    """Frozen oracle for the pre-rewrite two-statement win-rate query."""

    scoped_ids = _scoped_compra_ids(session, filters).subquery("company_scope")
    target_oferente = sa.select(Oferente.id).where(
        Oferente.compra_id == Compra.id,
        _document_pair_match(Oferente, company_type, company_number),
    )
    target_adjudicacion = sa.select(Adjudicacion.id).where(
        Adjudicacion.compra_id == Compra.id,
        _document_pair_match(Adjudicacion, company_type, company_number),
    )
    participation_stmt = sa.select(sa.func.count(sa.func.distinct(Compra.id))).where(
        Compra.id.in_(sa.select(scoped_ids.c.id)),
        sa.or_(target_oferente.exists(), target_adjudicacion.exists()),
    )
    wins_stmt = sa.select(
        sa.func.count(sa.func.distinct(Adjudicacion.compra_id))
    ).where(
        Adjudicacion.compra_id.in_(sa.select(scoped_ids.c.id)),
        _document_pair_match(Adjudicacion, company_type, company_number),
    )
    participations = int(session.execute(participation_stmt).scalar_one() or 0)
    wins = int(session.execute(wins_stmt).scalar_one() or 0)
    rate = Decimal(wins) / Decimal(participations) if participations else None
    return participations, wins, rate


def _seed_company_win_rate_edges(
    make_compra, add_adj, make_oferente
) -> tuple[str, str]:
    """Seed every inclusive win-rate edge without relying on the new query."""

    target = ("RUT", "WIN-RATE-EDGES")

    def bid(compra: Compra, pair: tuple[str | None, str | None]) -> None:
        make_oferente(
            compra.id,
            tipo_doc_prov=pair[0],
            nro_doc_prov=pair[1],
        )

    offered_only = make_compra(fecha_pub_adj=date(2024, 1, 1))
    bid(offered_only, target)

    awarded_only = make_compra(fecha_pub_adj=date(2024, 1, 2))
    add_adj(
        awarded_only,
        nombre_comercial="Awarded only",
        tipo_doc_prov=target[0],
        nro_doc_prov=target[1],
    )

    both_sides = make_compra(fecha_pub_adj=date(2024, 1, 3))
    bid(both_sides, target)
    for article in ("First line", "Second line"):
        add_adj(
            both_sides,
            nombre_comercial="Both sides",
            tipo_doc_prov=target[0],
            nro_doc_prov=target[1],
            desc_articulo=article,
        )

    duplicate_offers = make_compra(fecha_pub_adj=date(2024, 1, 4))
    bid(duplicate_offers, target)
    bid(duplicate_offers, target)

    null_pair = make_compra(fecha_pub_adj=date(2024, 1, 5))
    bid(null_pair, (None, target[1]))

    out_of_year = make_compra(fecha_pub_adj=date(2023, 12, 31))
    bid(out_of_year, target)
    add_adj(
        out_of_year,
        nombre_comercial="Out of year",
        tipo_doc_prov=target[0],
        nro_doc_prov=target[1],
    )
    return target


# ---------------------------------------------------------------------------
# top_articles
# ---------------------------------------------------------------------------


class TestTopArticles:
    """Top article totals use the exact company scope and active dates."""

    def test_groups_by_id_or_description_and_excludes_null_amounts(
        self, db_session, make_compra, add_adj
    ) -> None:
        company = ("RUT", "TOP-ARTICLE-A")

        first = make_compra(fecha_pub_adj=date(2024, 3, 1))
        add_adj(
            first,
            tipo_doc_prov=company[0],
            nro_doc_prov=company[1],
            id_articulo="100",
            desc_articulo="Shared article",
            amount_uyu=Decimal("500.00"),
        )
        second = make_compra(fecha_pub_adj=date(2024, 3, 2))
        add_adj(
            second,
            tipo_doc_prov=company[0],
            nro_doc_prov=company[1],
            id_articulo="100",
            desc_articulo="Updated description",
            amount_uyu=Decimal("300.00"),
        )
        description_only = make_compra(fecha_pub_adj=date(2024, 3, 3))
        add_adj(
            description_only,
            tipo_doc_prov=company[0],
            nro_doc_prov=company[1],
            id_articulo=None,
            desc_articulo="Shared article",
            amount_uyu=Decimal("700.00"),
        )
        null_amount = make_compra(fecha_pub_adj=date(2024, 3, 4))
        add_adj(
            null_amount,
            tipo_doc_prov=company[0],
            nro_doc_prov=company[1],
            id_articulo="null-only",
            desc_articulo="Ignored article",
            amount_uyu=None,
        )

        result = top_articles(
            db_session,
            AdjudicationFilters(
                company_doc_exact=company,
                date_from=date(2024, 1, 1),
                date_to=date(2024, 12, 31),
            ),
        )

        assert [(row.article_id, row.name, row.total_amount_uyu) for row in result] == [
            ("100", "Shared article", Decimal("800.00")),
            (None, "Shared article", Decimal("700.00")),
        ]
        assert [row.adjudication_count for row in result] == [2, 1]

    def test_scope_date_filter_order_and_cap(
        self, db_session, make_compra, add_adj
    ) -> None:
        company = ("RUT", "TOP-ARTICLE-A")
        for index in range(1, 22):
            compra = make_compra(fecha_pub_adj=date(2024, 4, 1))
            add_adj(
                compra,
                tipo_doc_prov=company[0],
                nro_doc_prov=company[1],
                id_articulo=f"article-{index}",
                desc_articulo=f"Article {index}",
                amount_uyu=Decimal(index * 100),
            )

        other_company = make_compra(fecha_pub_adj=date(2024, 4, 1))
        add_adj(
            other_company,
            tipo_doc_prov="RUT",
            nro_doc_prov="TOP-ARTICLE-B",
            id_articulo="other",
            desc_articulo="Other company article",
            amount_uyu=Decimal("99999.00"),
        )
        previous_year = make_compra(fecha_pub_adj=date(2023, 4, 1))
        add_adj(
            previous_year,
            tipo_doc_prov=company[0],
            nro_doc_prov=company[1],
            id_articulo="previous-year",
            desc_articulo="Previous year article",
            amount_uyu=Decimal("88888.00"),
        )

        result = top_articles(
            db_session,
            AdjudicationFilters(
                company_doc_exact=company,
                date_from=date(2024, 1, 1),
                date_to=date(2024, 12, 31),
            ),
            limit=100,
        )

        assert len(result) == 20
        assert result[0].article_id == "article-21"
        assert result[0].total_amount_uyu == Decimal("2100.00")
        assert result[-1].article_id == "article-2"
        assert all(row.article_id != "article-1" for row in result)
        assert all(row.article_id not in {"other", "previous-year"} for row in result)


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

    def test_total_matches_full_filtered_set_not_listing_page(
        self, db_session, make_compra, add_adj
    ) -> None:
        for index in range(21):
            compra = make_compra(organismo="OSE")
            add_adj(
                compra,
                nombre_comercial=f"Company {index}",
                amount_uyu=None if index == 0 else Decimal("100.00"),
            )
        other = make_compra(organismo="Other")
        add_adj(other, nombre_comercial="Outside filter")

        filters = AdjudicationFilters(organism="OSE")
        result = kpi_summary(db_session, filters)

        assert result.total == count_adjudications(db_session, filters) == 21
        assert len(list_adjudications(db_session, filters, limit=10, offset=0)) == 10
        assert len(list_adjudications(db_session, filters, limit=10, offset=10)) == 10
        assert len(list_adjudications(db_session, filters, limit=10, offset=20)) == 1
        assert result.total == 21

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
        assert result.total == 2
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
        assert result.total == 0

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

    def test_purchase_with_off_filter_adjudication_is_excluded(
        self, db_session, make_compra, add_adj, add_oferente
    ) -> None:
        matching = make_compra()
        outside_scope = make_compra()
        add_adj(matching, nombre_comercial="Target company")
        add_adj(outside_scope, nombre_comercial="Other company")
        add_oferente(matching)
        add_oferente(outside_scope)
        add_oferente(outside_scope, nombre_comercial="Second bidder")

        result = concentration_ratio(
            db_session, AdjudicationFilters(company="Target company")
        )

        assert result.single_bidder_count == 1
        assert result.multi_bidder_count == 0
        assert result.ratio == Decimal("1")

    def test_null_amount_matching_adjudication_still_classifies_purchase(
        self, db_session, make_compra, add_adj, add_oferente
    ) -> None:
        compra = make_compra()
        add_adj(compra, amount_uyu=None)
        add_oferente(compra)
        add_oferente(compra, nombre_comercial="Second bidder")

        result = concentration_ratio(db_session, AdjudicationFilters())

        assert result.single_bidder_count == 0
        assert result.multi_bidder_count == 1
        assert result.ratio == Decimal("0")

    def test_date_range_scopes_concentration_to_matching_purchases(
        self, db_session, make_compra, add_adj, add_oferente
    ) -> None:
        in_range = make_compra(fecha_pub_adj=date(2026, 6, 15))
        outside_range = make_compra(fecha_pub_adj=date(2025, 12, 31))
        add_adj(in_range)
        add_adj(outside_range)
        add_oferente(in_range)
        add_oferente(outside_range)
        add_oferente(outside_range, nombre_comercial="Second bidder")

        result = concentration_ratio(
            db_session,
            AdjudicationFilters(date_from=date(2026, 1, 1), date_to=date(2026, 12, 31)),
        )

        assert result.single_bidder_count == 1
        assert result.multi_bidder_count == 0
        assert result.ratio == Decimal("1")

    def test_company_document_exact_scopes_concentration(
        self, db_session, make_compra, add_adj, add_oferente
    ) -> None:
        matching = make_compra()
        outside_scope = make_compra()
        add_adj(matching, tipo_doc_prov="RUT", nro_doc_prov="111")
        add_adj(outside_scope, tipo_doc_prov="RUT", nro_doc_prov="222")
        add_oferente(matching)
        add_oferente(outside_scope)
        add_oferente(outside_scope, nombre_comercial="Second bidder")

        result = concentration_ratio(
            db_session,
            AdjudicationFilters(company_doc_exact=("RUT", "111")),
        )

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
        deduplicate the Compra before bucketing — otherwise counting
        through each Adjudicacion would over-count the oferentes.
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


# ---------------------------------------------------------------------------
# Company document identity and profile aggregates
# ---------------------------------------------------------------------------


def test_company_doc_exact_requires_both_document_columns(
    db_session, make_compra, add_adj
) -> None:
    first = make_compra()
    second = make_compra()
    third = make_compra()
    add_adj(first, nombre_comercial="Exact", tipo_doc_prov="RUT", nro_doc_prov="0123")
    add_adj(
        second, nombre_comercial="Other type", tipo_doc_prov="CI", nro_doc_prov="0123"
    )
    add_adj(
        third, nombre_comercial="Other number", tipo_doc_prov="RUT", nro_doc_prov="123"
    )

    rows = list_adjudications(
        db_session,
        AdjudicationFilters(company_doc_exact=("RUT", "0123")),
    )

    assert [row.winning_company for row in rows] == ["Exact"]


def test_name_filter_still_includes_rows_without_document_identity(
    db_session, make_compra, add_adj
) -> None:
    compra = make_compra()
    add_adj(
        compra,
        nombre_comercial="Name-only company",
        tipo_doc_prov=None,
        nro_doc_prov=None,
    )

    assert (
        len(list_adjudications(db_session, AdjudicationFilters(company="Name-only")))
        == 1
    )
    assert (
        list_adjudications(
            db_session,
            AdjudicationFilters(company_doc_exact=("RUT", "missing")),
        )
        == []
    )


def test_lookup_company_identity_uses_latest_date_then_id(
    db_session, make_compra, add_adj
) -> None:
    older = make_compra(fecha_pub_adj=date(2024, 1, 1))
    newer = make_compra(fecha_pub_adj=date(2024, 2, 1))
    tied = make_compra(fecha_pub_adj=date(2024, 2, 1))
    add_adj(older, nombre_comercial="Old", nro_doc_prov="42")
    add_adj(newer, nombre_comercial="New by date", nro_doc_prov="42")
    add_adj(tied, nombre_comercial="New by id", nro_doc_prov="42")

    assert lookup_company_identity(db_session, "RUT", "42") == "New by id"
    assert lookup_company_identity(db_session, "RUT", "missing") is None


def test_company_summary_counts_distinct_purchases_and_excludes_null_amounts(
    db_session, make_compra, add_adj
) -> None:
    company_purchase = make_compra(organismo="OSE", fecha_pub_adj=date(2024, 3, 1))
    company_purchase_two = make_compra(organismo="ASSE", fecha_pub_adj=date(2024, 4, 1))
    other_purchase = make_compra(organismo="OSE", fecha_pub_adj=date(2024, 5, 1))
    for amount, article, compra in (
        (100, "Laptop", company_purchase),
        (50, "Monitor", company_purchase),
    ):
        add_adj(
            compra,
            nombre_comercial="ACME",
            nro_doc_prov="42",
            desc_articulo=article,
            amount_uyu=Decimal(amount),
        )
    add_adj(
        company_purchase_two,
        nombre_comercial="ACME",
        nro_doc_prov="42",
        amount_uyu=None,
    )
    add_adj(
        other_purchase,
        nombre_comercial="Other",
        nro_doc_prov="99",
        amount_uyu=Decimal("150"),
    )

    result = company_summary(
        db_session,
        AdjudicationFilters(
            company_doc_exact=("RUT", "42"),
            date_from=date(2024, 1, 1),
            date_to=date(2024, 12, 31),
        ),
    )

    assert isinstance(result, CompanyProfileSummary)
    assert result.total_amount == Decimal("150.00")
    assert result.purchase_count == 2
    assert result.organism_count == 2
    assert result.share_of_total == Decimal("0.5")


def test_company_summary_total_equals_count_adjudications(
    db_session, make_compra, add_adj
) -> None:
    company_filters = AdjudicationFilters(
        company_doc_exact=("RUT", "42"),
        date_from=date(2024, 1, 1),
        date_to=date(2024, 12, 31),
    )
    for index in range(21):
        compra = make_compra(fecha_pub_adj=date(2024, 1, index + 1))
        add_adj(
            compra,
            nombre_comercial="ACME",
            tipo_doc_prov="RUT",
            nro_doc_prov="42",
            amount_uyu=Decimal("10.00"),
        )
    other = make_compra(fecha_pub_adj=date(2024, 1, 31))
    add_adj(
        other,
        nombre_comercial="Other",
        tipo_doc_prov="RUT",
        nro_doc_prov="99",
        amount_uyu=Decimal("20.00"),
    )

    summary = company_summary(db_session, company_filters)

    assert summary.total == count_adjudications(db_session, company_filters) == 21

    empty_filters = AdjudicationFilters(
        company_doc_exact=("RUT", "missing"),
        date_from=date(2024, 1, 1),
        date_to=date(2024, 12, 31),
    )
    empty_summary = company_summary(db_session, empty_filters)
    assert empty_summary.total == count_adjudications(db_session, empty_filters) == 0


def test_company_summary_provided_market_total_skips_market_query(
    db_session, make_compra, add_adj, monkeypatch
) -> None:
    company_filters = AdjudicationFilters(
        company_doc_exact=("RUT", "42"),
        date_from=date(2024, 1, 1),
        date_to=date(2024, 12, 31),
    )
    for amount, document in ((75, "42"), (75, "42"), (150, "99")):
        compra = make_compra(fecha_pub_adj=date(2024, 1, amount // 75))
        add_adj(
            compra,
            nombre_comercial="ACME" if document == "42" else "Other",
            tipo_doc_prov="RUT",
            nro_doc_prov=document,
            amount_uyu=Decimal(amount),
        )

    original_execute = db_session.execute
    execute_count = 0

    def one_execute_only(statement, *args, **kwargs):
        nonlocal execute_count
        execute_count += 1
        if execute_count > 1:
            raise AssertionError("provided market_total must skip market SUM")
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", one_execute_only)

    provided = company_summary(db_session, company_filters, market_total=Decimal("300"))
    assert provided.total == 2
    assert provided.share_of_total == Decimal("0.5")

    execute_count = 0
    zero_market = company_summary(
        db_session, company_filters, market_total=Decimal("0")
    )
    assert zero_market.share_of_total == Decimal("0")

    monkeypatch.setattr(db_session, "execute", original_execute)
    self_computed = company_summary(db_session, company_filters)
    assert self_computed.share_of_total == Decimal("0.5")


def test_company_win_rate_is_inclusive_null_safe_and_date_scoped(
    db_session, make_compra, add_adj, make_oferente
) -> None:
    company = ("RUT", "WIN-RATE")

    def add_purchase(when: date, offered: bool, awarded: bool) -> None:
        compra = make_compra(fecha_pub_adj=when)
        if offered:
            make_oferente(compra.id, tipo_doc_prov=company[0], nro_doc_prov=company[1])
        if awarded:
            add_adj(
                compra,
                nombre_comercial="Winner",
                tipo_doc_prov=company[0],
                nro_doc_prov=company[1],
            )

    for when, offered, awarded in (
        (date(2024, 1, 1), True, True),
        (date(2024, 1, 2), True, False),
        (date(2024, 1, 3), False, True),
        (date(2023, 1, 1), True, True),
    ):
        add_purchase(when, offered, awarded)

    null_purchase = make_compra(fecha_pub_adj=date(2024, 1, 4))
    make_oferente(null_purchase.id, tipo_doc_prov=None, nro_doc_prov=company[1])

    result = company_win_rate(
        db_session,
        *company,
        AdjudicationFilters(date_from=date(2024, 1, 1), date_to=date(2024, 12, 31)),
    )

    assert (result.participations, result.wins) == (3, 2)
    assert result.wins <= result.participations
    assert result.rate == Decimal("2") / Decimal("3")
    assert company_win_rate(
        db_session, "RUT", "MISSING", AdjudicationFilters()
    ) == CompanyWinRate(0, 0, None)


def test_company_win_rate_matches_legacy_oracle_on_multi_edge_fixture(
    db_session, make_compra, add_adj, make_oferente
) -> None:
    company = _seed_company_win_rate_edges(make_compra, add_adj, make_oferente)
    filters = AdjudicationFilters(
        date_from=date(2024, 1, 1), date_to=date(2024, 12, 31)
    )

    expected = _legacy_company_win_rate_tuples(db_session, *company, filters)
    result = company_win_rate(db_session, *company, filters)

    assert expected == (4, 2, Decimal("2") / Decimal("4"))
    assert (result.participations, result.wins, result.rate) == expected


def test_company_win_rate_matches_legacy_oracle_on_zero_activity(db_session) -> None:
    filters = AdjudicationFilters(
        date_from=date(2024, 1, 1), date_to=date(2024, 12, 31)
    )

    expected = _legacy_company_win_rate_tuples(
        db_session, "RUT", "NO-ACTIVITY", filters
    )
    result = company_win_rate(db_session, "RUT", "NO-ACTIVITY", filters)

    assert expected == (0, 0, None)
    assert (result.participations, result.wins, result.rate) == expected


def test_company_win_rate_matches_legacy_oracle_on_single_offered_edge(
    db_session, make_compra, make_oferente
) -> None:
    company = ("RUT", "OFFERED-ONLY")
    compra = make_compra(fecha_pub_adj=date(2024, 6, 1))
    make_oferente(compra.id, tipo_doc_prov=company[0], nro_doc_prov=company[1])
    filters = AdjudicationFilters(
        date_from=date(2024, 1, 1), date_to=date(2024, 12, 31)
    )

    expected = _legacy_company_win_rate_tuples(db_session, *company, filters)
    result = company_win_rate(db_session, *company, filters)

    assert expected == (1, 0, Decimal(0))
    assert (result.participations, result.wins, result.rate) == expected


def test_company_win_rate_matches_legacy_oracle_on_rate_math(
    db_session, make_compra, add_adj, make_oferente
) -> None:
    company = ("RUT", "RATE-MATH")
    both = make_compra(fecha_pub_adj=date(2024, 7, 1))
    make_oferente(both.id, tipo_doc_prov=company[0], nro_doc_prov=company[1])
    add_adj(
        both,
        nombre_comercial="Rate math winner",
        tipo_doc_prov=company[0],
        nro_doc_prov=company[1],
    )
    offered = make_compra(fecha_pub_adj=date(2024, 7, 2))
    make_oferente(offered.id, tipo_doc_prov=company[0], nro_doc_prov=company[1])
    awarded = make_compra(fecha_pub_adj=date(2024, 7, 3))
    add_adj(
        awarded,
        nombre_comercial="Rate math winner",
        tipo_doc_prov=company[0],
        nro_doc_prov=company[1],
    )
    filters = AdjudicationFilters(
        date_from=date(2024, 1, 1), date_to=date(2024, 12, 31)
    )

    expected = _legacy_company_win_rate_tuples(db_session, *company, filters)
    result = company_win_rate(db_session, *company, filters)

    assert expected == (3, 2, Decimal(2) / Decimal(3))
    assert (result.participations, result.wins, result.rate) == expected


def test_company_win_rate_runs_one_execute(
    db_session, make_compra, add_adj, make_oferente
) -> None:
    company = _seed_company_win_rate_edges(make_compra, add_adj, make_oferente)
    filters = AdjudicationFilters(
        date_from=date(2024, 1, 1), date_to=date(2024, 12, 31)
    )

    class ExecuteCountingSession:
        def __init__(self, wrapped: Session) -> None:
            self.wrapped = wrapped
            self.execute_calls = 0

        def execute(self, *args, **kwargs):
            self.execute_calls += 1
            return self.wrapped.execute(*args, **kwargs)

    counted_session = ExecuteCountingSession(db_session)
    result = company_win_rate(cast("Session", counted_session), *company, filters)

    assert (result.participations, result.wins) == (4, 2)
    assert counted_session.execute_calls == 1


def test_company_competitors_ranks_top_five_and_resolves_names_in_batch(
    db_session, make_compra, add_adj, make_oferente
) -> None:
    target = ("RUT", "TARGET")
    competitor_counts = {"A": 3, "B": 3, "C": 2, "D": 1, "E": 1, "F": 2}
    for suffix, count in competitor_counts.items():
        for index in range(count):
            compra = make_compra(fecha_pub_adj=date(2024, 1, index + 1))
            make_oferente(compra.id, tipo_doc_prov=target[0], nro_doc_prov=target[1])
            make_oferente(
                compra.id,
                nombre_comercial=f"Fallback {suffix}",
                tipo_doc_prov="RUT",
                nro_doc_prov=f"COMP-{suffix}",
            )
            if suffix == "A" and index == 0:
                make_oferente(compra.id, tipo_doc_prov=None, nro_doc_prov="NULL")
            if suffix != "F":
                add_adj(
                    compra,
                    nombre_comercial=f"Canonical {suffix}",
                    tipo_doc_prov="RUT",
                    nro_doc_prov=f"COMP-{suffix}",
                    amount_uyu=Decimal("200" if suffix == "B" else "100"),
                )

    extra = make_compra(fecha_pub_adj=date(2023, 1, 1))
    make_oferente(extra.id, tipo_doc_prov=target[0], nro_doc_prov=target[1])
    make_oferente(extra.id, tipo_doc_prov="RUT", nro_doc_prov="COMP-OUT")

    result = company_competitors(
        db_session,
        *target,
        AdjudicationFilters(date_from=date(2024, 1, 1), date_to=date(2024, 12, 31)),
    )

    assert [(row.display_name, row.purchase_count) for row in result] == [
        ("Canonical B", 3),
        ("Canonical A", 3),
        ("Canonical C", 2),
        ("Fallback F", 2),
        ("Canonical D", 1),
    ]


def test_company_competitors_matches_frozen_oracle_on_multi_edge_fixture(
    db_session, make_compra, add_adj, make_oferente
) -> None:
    target = _seed_company_competitor_edges(make_compra, add_adj, make_oferente)
    filters = AdjudicationFilters(
        date_from=date(2024, 1, 1), date_to=date(2024, 12, 31)
    )

    expected = _legacy_company_competitor_tuples(db_session, *target, filters)
    actual = [
        (
            row.company_type,
            row.company_number,
            row.purchase_count,
            row.awarded_amount_uyu,
            row.display_name,
        )
        for row in company_competitors(db_session, *target, filters)
    ]

    assert expected == [
        ("RUT", "COMP-A", 2, Decimal("175.00"), "Canonical A"),
        ("RUT", "COMP-B", 2, Decimal("80.00"), "Canonical B"),
        ("RUT", "COMP-F", 1, Decimal("0"), "Fallback F"),
    ]
    assert actual == expected


def test_company_competitors_returns_empty_for_target_without_co_bidders(
    db_session, make_compra, make_oferente
) -> None:
    target = ("RUT", "TARGET-EMPTY")
    compra = make_compra(fecha_pub_adj=date(2024, 2, 1))
    make_oferente(compra.id, tipo_doc_prov=target[0], nro_doc_prov=target[1])
    filters = AdjudicationFilters(
        date_from=date(2024, 1, 1), date_to=date(2024, 12, 31)
    )

    expected = _legacy_company_competitor_tuples(db_session, *target, filters)
    actual = company_competitors(db_session, *target, filters)

    assert expected == []
    assert actual == []


def test_company_competitors_matches_oracle_for_single_competitor(
    db_session, make_compra, add_adj, make_oferente
) -> None:
    target = ("RUT", "TARGET-SINGLE")
    compra = make_compra(fecha_pub_adj=date(2024, 2, 1))
    make_oferente(compra.id, tipo_doc_prov=target[0], nro_doc_prov=target[1])
    make_oferente(
        compra.id,
        nombre_comercial="Fallback single",
        tipo_doc_prov="CI",
        nro_doc_prov="SINGLE",
    )
    add_adj(
        compra,
        nombre_comercial="Canonical single",
        tipo_doc_prov="CI",
        nro_doc_prov="SINGLE",
        amount_uyu=Decimal("42.00"),
    )
    filters = AdjudicationFilters(
        date_from=date(2024, 1, 1), date_to=date(2024, 12, 31)
    )

    expected = _legacy_company_competitor_tuples(db_session, *target, filters)
    actual = [
        (
            row.company_type,
            row.company_number,
            row.purchase_count,
            row.awarded_amount_uyu,
            row.display_name,
        )
        for row in company_competitors(db_session, *target, filters)
    ]

    assert expected == [("CI", "SINGLE", 1, Decimal("42.00"), "Canonical single")]
    assert actual == expected


def test_company_competitors_excludes_award_only_document_pair(
    db_session, make_compra, add_adj, make_oferente
) -> None:
    target = _seed_company_competitor_edges(make_compra, add_adj, make_oferente)
    filters = AdjudicationFilters(
        date_from=date(2024, 1, 1), date_to=date(2024, 12, 31)
    )

    result = company_competitors(db_session, *target, filters)

    assert [row.company_number for row in result] == ["COMP-A", "COMP-B", "COMP-F"]


def test_company_competitors_transparent_to_composite_index(
    db_session, make_compra, add_adj, make_oferente
) -> None:
    target = _seed_company_competitor_edges(make_compra, add_adj, make_oferente)
    filters = AdjudicationFilters(
        date_from=date(2024, 1, 1), date_to=date(2024, 12, 31)
    )
    without_index = [
        (
            row.company_type,
            row.company_number,
            row.purchase_count,
            row.awarded_amount_uyu,
            row.display_name,
        )
        for row in company_competitors(db_session, *target, filters)
    ]
    index = sa.Index(
        "ix_adjudicacion_compra_document",
        Adjudicacion.compra_id,
        Adjudicacion.tipo_doc_prov,
        Adjudicacion.nro_doc_prov,
    )
    try:
        index.create(bind=db_session.get_bind())
        with_index = [
            (
                row.company_type,
                row.company_number,
                row.purchase_count,
                row.awarded_amount_uyu,
                row.display_name,
            )
            for row in company_competitors(db_session, *target, filters)
        ]
        assert with_index == without_index
    finally:
        index.drop(bind=db_session.get_bind())


def test_company_widgets_and_listing_are_scoped_by_document(
    db_session, make_compra, add_adj, add_oferente
) -> None:
    company_compra = make_compra(organismo="OSE", fecha_pub_adj=date(2024, 1, 1))
    other_compra = make_compra(organismo="ASSE", fecha_pub_adj=date(2024, 1, 1))
    add_adj(
        company_compra,
        nombre_comercial="ACME",
        nro_doc_prov="42",
        amount_uyu=Decimal("100"),
    )
    add_adj(
        other_compra,
        nombre_comercial="Other",
        nro_doc_prov="99",
        amount_uyu=Decimal("900"),
    )
    add_oferente(company_compra)
    add_oferente(other_compra)
    filters = AdjudicationFilters(company_doc_exact=("RUT", "42"))

    assert monthly_trend(db_session, filters) == [("2024-01", Decimal("100.00"))]
    assert [entry.name for entry in ranking_by_organism(db_session, filters)] == ["OSE"]
    assert concentration_ratio(db_session, filters).single_bidder_count == 1
    assert len(list_adjudications(db_session, filters)) == 1


def test_ranking_by_company_attaches_the_dominant_document_pair(
    db_session, make_compra, add_adj
) -> None:
    for number in ("A", "A", "A", "B"):
        compra = make_compra()
        add_adj(
            compra,
            nombre_comercial="ACME",
            tipo_doc_prov="RUT",
            nro_doc_prov=number,
            amount_uyu=Decimal("100"),
        )

    entry = ranking_by_company(db_session, AdjudicationFilters())[0]

    assert entry.company_type == "RUT"
    assert entry.company_number == "A"
    assert entry.company_profile_url == "/company/RUT/A"


def test_ranking_by_company_tie_breaks_by_latest_date_then_lexicographically(
    db_session, make_compra, add_adj
) -> None:
    for pair, when in (
        (("RUT", "2"), date(2024, 3, 1)),
        (("RUT", "2"), date(2024, 3, 2)),
        (("CI", "9"), date(2024, 4, 1)),
        (("CI", "9"), date(2024, 4, 2)),
    ):
        compra = make_compra(fecha_pub_adj=when)
        add_adj(
            compra,
            nombre_comercial="TIE",
            tipo_doc_prov=pair[0],
            nro_doc_prov=pair[1],
            amount_uyu=Decimal("100"),
        )

    entry = ranking_by_company(db_session, AdjudicationFilters())[0]

    assert (entry.company_type, entry.company_number) == ("CI", "9")


def test_ranking_by_company_leaves_missing_document_identity_unlinked(
    db_session, make_compra, add_adj
) -> None:
    compra = make_compra()
    add_adj(
        compra,
        nombre_comercial="NAME-ONLY",
        tipo_doc_prov=None,
        nro_doc_prov=None,
        amount_uyu=Decimal("100"),
    )

    entry = ranking_by_company(db_session, AdjudicationFilters())[0]

    assert entry.company_type is None
    assert entry.company_number is None
    assert entry.company_profile_url is None


def test_ranking_by_company_uses_lexical_pair_as_final_tie_break(
    db_session, make_compra, add_adj
) -> None:
    for pair in (("RUT", "2"), ("CI", "9")):
        for _ in range(2):
            compra = make_compra(fecha_pub_adj=date(2024, 5, 1))
            add_adj(
                compra,
                nombre_comercial="LEXICAL-TIE",
                tipo_doc_prov=pair[0],
                nro_doc_prov=pair[1],
                amount_uyu=Decimal("100"),
            )

    entry = ranking_by_company(db_session, AdjudicationFilters())[0]

    assert (entry.company_type, entry.company_number) == ("CI", "9")


def _add_raw_ranking_adjudication(
    db_session: Session,
    compra: Compra,
    *,
    name: str,
    company_type: str | None,
    company_number: str | None,
    amount: Decimal | None,
    article: str,
) -> Adjudicacion:
    """Insert an unnormalized ranking row, including empty document values."""

    adjudication = Adjudicacion(
        compra_id=compra.id,
        nombre_comercial=name,
        tipo_doc_prov=company_type,
        nro_doc_prov=company_number,
        desc_articulo=article,
        id_moneda=0,
        precio_tot_imp=amount or Decimal("0.00"),
        amount_uyu=amount,
    )
    db_session.add(adjudication)
    db_session.flush()
    return adjudication


def _ranking_tuples(entries) -> list[tuple[str, Decimal, int, str | None, str | None]]:
    return [
        (
            entry.name,
            entry.total_amount_uyu,
            entry.adjudication_count,
            entry.company_type,
            entry.company_number,
        )
        for entry in entries
    ]


def _semantic_ranking_oracle(
    session: Session,
    rows: list[Adjudicacion],
    *,
    limit: int,
) -> list[tuple[str, Decimal, int, str | None, str | None]]:
    """Compute ranking semantics in Python, independently of SQL shape."""

    grouped: dict[tuple[str, str | None, str | None], tuple[int, Decimal, date]] = {}
    for row in rows:
        amount = cast("Decimal | None", row.amount_uyu)
        if amount is None:
            continue
        key = (
            cast("str", row.nombre_comercial),
            cast("str | None", row.tipo_doc_prov),
            cast("str | None", row.nro_doc_prov),
        )
        compra = session.get(Compra, row.compra_id)
        assert compra is not None
        compra_date = cast("date", compra.fecha_pub_adj)
        count, total, latest = grouped.get(key, (0, Decimal("0.00"), compra_date))
        grouped[key] = (
            count + 1,
            total + amount,
            max(latest, compra_date),
        )

    by_name: dict[str, list[tuple[str | None, str | None, int, Decimal, date]]] = {}
    for (name, company_type, company_number), (count, total, latest) in grouped.items():
        by_name.setdefault(name, []).append(
            (company_type, company_number, count, total, latest)
        )

    results = []
    for name, identity_groups in by_name.items():
        winner = sorted(
            (
                group
                for group in identity_groups
                if group[0] not in (None, "") and group[1] not in (None, "")
            ),
            key=lambda group: (-group[2], -group[4].toordinal(), group[0], group[1]),
        )
        identity = winner[0] if winner else (None, None)
        results.append(
            (
                name,
                sum((group[3] for group in identity_groups), Decimal("0.00")),
                sum(group[2] for group in identity_groups),
                identity[0],
                identity[1],
            )
        )
    return sorted(results, key=lambda item: item[1], reverse=True)[:limit]


def test_ranking_by_company_rolls_up_multiple_identities(
    db_session, make_compra, add_adj
) -> None:
    first = make_compra(fecha_pub_adj=date(2024, 1, 1))
    second = make_compra(fecha_pub_adj=date(2024, 1, 2))
    add_adj(
        first,
        nombre_comercial="MULTI-ID",
        tipo_doc_prov="RUT",
        nro_doc_prov="1",
        amount_uyu=Decimal("1000.00"),
    )
    add_adj(
        second,
        nombre_comercial="MULTI-ID",
        tipo_doc_prov="CI",
        nro_doc_prov="2",
        amount_uyu=Decimal("500.00"),
    )

    entry = ranking_by_company(db_session, AdjudicationFilters())[0]

    assert _ranking_tuples([entry]) == [("MULTI-ID", Decimal("1500.00"), 2, "CI", "2")]


def test_ranking_by_company_mixed_invalid_and_valid_documents_rolls_up_name(
    db_session, make_compra, add_adj
) -> None:
    compra = make_compra()
    _add_raw_ranking_adjudication(
        db_session,
        compra,
        name="MIXED-CO",
        company_type="",
        company_number="",
        amount=Decimal("1000.00"),
        article="invalid",
    )
    add_adj(
        compra,
        nombre_comercial="MIXED-CO",
        tipo_doc_prov="RUT",
        nro_doc_prov="99",
        amount_uyu=Decimal("500.00"),
    )

    result = ranking_by_company(db_session, AdjudicationFilters())

    assert _ranking_tuples(result) == [("MIXED-CO", Decimal("1500.00"), 2, "RUT", "99")]


def test_ranking_by_company_empty_string_identity_is_unlinked(
    db_session, make_compra
) -> None:
    _add_raw_ranking_adjudication(
        db_session,
        make_compra(),
        name="EMPTY-DOC",
        company_type="",
        company_number="",
        amount=Decimal("250.00"),
        article="empty",
    )

    result = ranking_by_company(db_session, AdjudicationFilters())

    assert _ranking_tuples(result) == [("EMPTY-DOC", Decimal("250.00"), 1, None, None)]


def test_ranking_by_company_identity_is_ranked_inside_filtered_set(
    db_session, make_compra, add_adj
) -> None:
    for index in range(3):
        add_adj(
            make_compra(organismo="OUTSIDE", fecha_pub_adj=date(2024, 3, index + 1)),
            nombre_comercial="FILTERED-ID",
            tipo_doc_prov="CI",
            nro_doc_prov="9",
            amount_uyu=Decimal("100.00"),
        )
    add_adj(
        make_compra(organismo="INSIDE", fecha_pub_adj=date(2024, 2, 1)),
        nombre_comercial="FILTERED-ID",
        tipo_doc_prov="RUT",
        nro_doc_prov="1",
        amount_uyu=Decimal("100.00"),
    )

    result = ranking_by_company(
        db_session,
        AdjudicationFilters(organism_exact="INSIDE"),
    )

    assert _ranking_tuples(result) == [
        ("FILTERED-ID", Decimal("100.00"), 1, "RUT", "1")
    ]


def test_ranking_by_company_excludes_null_amounts_from_total_and_count(
    db_session, make_compra, add_adj
) -> None:
    compra = make_compra()
    add_adj(
        compra,
        nombre_comercial="NULL-AMOUNT",
        tipo_doc_prov="RUT",
        nro_doc_prov="7",
        amount_uyu=Decimal("300.00"),
    )
    add_adj(
        compra,
        nombre_comercial="NULL-AMOUNT",
        tipo_doc_prov="RUT",
        nro_doc_prov="7",
        desc_articulo="unpriced",
        amount_uyu=None,
    )

    result = ranking_by_company(db_session, AdjudicationFilters())

    assert _ranking_tuples(result) == [
        ("NULL-AMOUNT", Decimal("300.00"), 1, "RUT", "7")
    ]


def test_ranking_by_company_limits_to_ten_names(
    db_session, make_compra, add_adj
) -> None:
    for index in range(12):
        add_adj(
            make_compra(),
            nombre_comercial=f"COMPANY-{index:02d}",
            tipo_doc_prov="RUT",
            nro_doc_prov=str(index),
            amount_uyu=Decimal(index + 1),
        )

    result = ranking_by_company(db_session, AdjudicationFilters(), limit=10)

    assert len(result) == 10
    assert [entry.name for entry in result] == [
        f"COMPANY-{index:02d}" for index in range(11, 1, -1)
    ]


@pytest.mark.parametrize(
    "rows",
    [
        [
            ("RUT", "1", Decimal("1000.00")),
            ("", "", Decimal("500.00")),
            ("CI", "2", None),
        ],
        [
            ("CI", "9", Decimal("75.00")),
            (None, None, Decimal("25.00")),
        ],
    ],
)
def test_ranking_by_company_matches_semantic_oracle(
    db_session, make_compra, add_adj, rows
) -> None:
    inserted = []
    for index, (company_type, company_number, amount) in enumerate(rows):
        compra = make_compra(fecha_pub_adj=date(2024, 1, index + 1))
        if company_type in (None, "") or company_number in (None, ""):
            row = _add_raw_ranking_adjudication(
                db_session,
                compra,
                name="ORACLE-CO",
                company_type=company_type,
                company_number=company_number,
                amount=amount,
                article=f"oracle-{index}",
            )
        else:
            row = add_adj(
                compra,
                nombre_comercial="ORACLE-CO",
                tipo_doc_prov=company_type,
                nro_doc_prov=company_number,
                amount_uyu=amount,
            )
        inserted.append(row)

    expected = _semantic_ranking_oracle(db_session, inserted, limit=10)

    assert (
        _ranking_tuples(ranking_by_company(db_session, AdjudicationFilters()))
        == expected
    )


def test_ranking_by_company_docstring_describes_name_rollup() -> None:
    docstring = ranking_by_company.__doc__ or ""

    assert "grouped" in docstring.lower()
    assert "identity" in docstring.lower()
    assert "rollup" in docstring.lower()
    assert "correlated" not in docstring.lower()
