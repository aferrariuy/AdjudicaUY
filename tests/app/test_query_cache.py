"""Tests for the process-local dashboard aggregate cache."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from app.services.filters import AdjudicationFilters
from app.services.query_cache import build_cache_key, cached_aggregate, clear_cache


def _settings(*, ttl: int = 600, max_entries: int = 256) -> SimpleNamespace:
    return SimpleNamespace(cache_ttl_seconds=ttl, cache_max_entries=max_entries)


def test_cache_key_normalizes_empty_values_and_distinguishes_active_values() -> None:
    empty = AdjudicationFilters(company=None, organism="", article_id="")
    equivalent = AdjudicationFilters(company="", organism=None, article_id=None)
    year = AdjudicationFilters(
        company_doc_exact=("RUT", "123"),
        date_from=date(2024, 1, 1),
        date_to=date(2024, 12, 31),
    )
    reordered = AdjudicationFilters(
        company_doc_exact=("123", "RUT"),
        date_from=date(2024, 1, 1),
        date_to=date(2024, 12, 31),
    )
    next_year = AdjudicationFilters(
        company_doc_exact=("RUT", "123"),
        date_from=date(2025, 1, 1),
        date_to=date(2025, 12, 31),
    )

    assert build_cache_key("kpi_summary", empty) == build_cache_key(
        "kpi_summary", equivalent
    )
    assert build_cache_key("kpi_summary", year) != build_cache_key(
        "kpi_summary", reordered
    )
    assert build_cache_key("kpi_summary", year) != build_cache_key(
        "kpi_summary", next_year
    )
    assert build_cache_key("ranking_by_company", year, limit=10) != build_cache_key(
        "ranking_by_company", year, limit=5
    )
    assert build_cache_key("kpi_summary", year, limit=10) == build_cache_key(
        "kpi_summary", year
    )
    for name in ("company_competitors", "company_win_rate"):
        assert build_cache_key(name, year, limit=10) != build_cache_key(
            name, year, limit=5
        )
        assert build_cache_key(name, year, limit=10) != build_cache_key(
            name, year, limit=None
        )


def test_cache_miss_then_hit_calls_aggregate_once(monkeypatch) -> None:
    monkeypatch.setattr("app.services.query_cache.get_settings", lambda: _settings())
    aggregate = Mock(return_value=["fresh"])
    filters = AdjudicationFilters(article="laptop")

    assert cached_aggregate("monthly_trend", aggregate, Mock(), filters) == ["fresh"]
    assert cached_aggregate("monthly_trend", aggregate, Mock(), filters) == ["fresh"]
    aggregate.assert_called_once()
    clear_cache()
    assert cached_aggregate("monthly_trend", aggregate, Mock(), filters) == ["fresh"]
    assert aggregate.call_count == 2


def test_cache_accepts_company_deep_path_aggregates(monkeypatch) -> None:
    monkeypatch.setattr("app.services.query_cache.get_settings", lambda: _settings())
    filters = AdjudicationFilters(company_doc_exact=("RUT", "42"))

    for name in (
        "company_win_rate",
        "company_competitors",
        "company_summary",
        "top_articles",
        "sitemap_xml",
    ):
        aggregate = Mock(return_value=name)

        assert cached_aggregate(name, aggregate, Mock(), filters) == name
        assert cached_aggregate(name, aggregate, Mock(), filters) == name
        aggregate.assert_called_once()


@pytest.mark.parametrize("ttl", [600, 0])
def test_cache_rejects_non_whitelisted_aggregates(monkeypatch, ttl: int) -> None:
    monkeypatch.setattr(
        "app.services.query_cache.get_settings", lambda: _settings(ttl=ttl)
    )

    for name in ("list_adjudications", "count_adjudications", "iter_adjudications"):
        aggregate = Mock()
        with pytest.raises(ValueError, match="not cacheable"):
            cached_aggregate(name, aggregate, Mock(), AdjudicationFilters())
        aggregate.assert_not_called()


def test_top_articles_limit_is_keyed_and_forwarded(monkeypatch) -> None:
    monkeypatch.setattr("app.services.query_cache.get_settings", lambda: _settings())
    filters = AdjudicationFilters(company_doc_exact=("RUT", "42"))

    assert build_cache_key("top_articles", filters, limit=10) != build_cache_key(
        "top_articles", filters, limit=20
    )
    assert build_cache_key("top_articles", filters, limit=10) != build_cache_key(
        "top_articles", filters, limit=None
    )

    aggregate = Mock(return_value=[])
    session = Mock()
    cached_aggregate("top_articles", aggregate, session, filters, limit=10)
    aggregate.assert_called_once_with(session, filters, limit=10)


def test_company_aggregate_limits_are_keyed_and_forwarded(monkeypatch) -> None:
    monkeypatch.setattr("app.services.query_cache.get_settings", lambda: _settings())
    filters = AdjudicationFilters(company_doc_exact=("RUT", "42"))

    for name in ("company_competitors", "company_win_rate"):
        assert build_cache_key(name, filters, limit=10) != build_cache_key(
            name, filters, limit=20
        )
        assert build_cache_key(name, filters, limit=10) != build_cache_key(
            name, filters, limit=None
        )

        aggregate = Mock(return_value=[])
        session = Mock()
        cached_aggregate(name, aggregate, session, filters, limit=10)
        aggregate.assert_called_once_with(session, filters, limit=10)


def test_zero_ttl_forwards_explicit_limit_for_limit_aware_aggregates(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.services.query_cache.get_settings", lambda: _settings(ttl=0)
    )
    filters = AdjudicationFilters(company_doc_exact=("RUT", "42"))

    for name in ("company_competitors", "company_win_rate"):
        aggregate = Mock(return_value=["fresh"])
        session = Mock()

        assert cached_aggregate(name, aggregate, session, filters, limit=10) == [
            "fresh"
        ]
        assert cached_aggregate(name, aggregate, session, filters, limit=10) == [
            "fresh"
        ]
        assert aggregate.call_count == 2
        aggregate.assert_has_calls(
            [
                call(session, filters, limit=10),
                call(session, filters, limit=10),
            ]
        )


def test_cache_expiry_reexecutes_after_ttl(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr("app.services.query_cache.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "app.services.query_cache.get_settings", lambda: _settings(ttl=10)
    )
    aggregate = Mock(side_effect=["first", "second"])
    filters = AdjudicationFilters()

    assert cached_aggregate("monthly_trend", aggregate, Mock(), filters) == "first"
    now[0] = 109.9
    assert cached_aggregate("monthly_trend", aggregate, Mock(), filters) == "first"
    now[0] = 110.1
    assert cached_aggregate("monthly_trend", aggregate, Mock(), filters) == "second"
    assert aggregate.call_count == 2


def test_zero_ttl_bypasses_lookup_and_storage(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.query_cache.get_settings", lambda: _settings(ttl=0)
    )
    aggregate = Mock(side_effect=["first", "second"])
    filters = AdjudicationFilters()

    assert cached_aggregate("kpi_summary", aggregate, Mock(), filters) == "first"
    assert cached_aggregate("kpi_summary", aggregate, Mock(), filters) == "second"
    assert aggregate.call_count == 2


def test_zero_ttl_bypasses_storage_for_company_deep_paths(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.query_cache.get_settings", lambda: _settings(ttl=0)
    )
    filters = AdjudicationFilters(company_doc_exact=("RUT", "42"))

    for name in (
        "company_win_rate",
        "company_competitors",
        "company_summary",
        "top_articles",
    ):
        aggregate = Mock(side_effect=["first", "second"])

        assert cached_aggregate(name, aggregate, Mock(), filters, limit=10) == "first"
        assert cached_aggregate(name, aggregate, Mock(), filters, limit=10) == "second"
        assert aggregate.call_count == 2


def test_cache_evicts_least_recently_used_entry(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.query_cache.get_settings", lambda: _settings(max_entries=2)
    )
    aggregate = Mock(side_effect=lambda _session, filters: filters.article)
    first = AdjudicationFilters(article="first")
    second = AdjudicationFilters(article="second")
    third = AdjudicationFilters(article="third")

    cached_aggregate("kpi_summary", aggregate, Mock(), first)
    cached_aggregate("kpi_summary", aggregate, Mock(), second)
    cached_aggregate("kpi_summary", aggregate, Mock(), first)
    cached_aggregate("kpi_summary", aggregate, Mock(), third)
    cached_aggregate("kpi_summary", aggregate, Mock(), second)
    assert aggregate.call_count == 4
