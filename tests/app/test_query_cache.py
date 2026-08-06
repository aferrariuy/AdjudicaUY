"""Tests for the process-local dashboard aggregate cache."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.adjudication_service import AdjudicationFilters
from app.services.query_cache import (
    build_cache_key,
    cached_aggregate,
    clear_cache,
)


def _settings(*, ttl: int = 600, max_entries: int = 256) -> SimpleNamespace:
    return SimpleNamespace(cache_ttl_seconds=ttl, cache_max_entries=max_entries)


def test_cache_key_normalizes_inactive_fields_and_preserves_document_order() -> None:
    empty = AdjudicationFilters(
        company=None,
        company_doc_exact=None,
        organism="",
        article=None,
        article_id="",
    )
    equivalent = AdjudicationFilters(
        company="",
        company_doc_exact=("RUT", "123"),
        organism=None,
        article="",
        article_id=None,
        date_from=date(2024, 1, 1),
        date_to=date(2024, 12, 31),
    )
    reordered = AdjudicationFilters(
        company="",
        company_doc_exact=("123", "RUT"),
        organism=None,
        article="",
        article_id=None,
        date_from=date(2024, 1, 1),
        date_to=date(2024, 12, 31),
    )

    assert build_cache_key("kpi_summary", empty) != build_cache_key(
        "kpi_summary", equivalent
    )
    assert build_cache_key("kpi_summary", equivalent) != build_cache_key(
        "kpi_summary", reordered
    )
    assert build_cache_key("kpi_summary", empty) == build_cache_key(
        "kpi_summary", AdjudicationFilters()
    )


def test_cache_key_distinguishes_years_and_limit_values() -> None:
    year_2024 = AdjudicationFilters(
        date_from=date(2024, 1, 1), date_to=date(2024, 12, 31)
    )
    year_2025 = AdjudicationFilters(
        date_from=date(2025, 1, 1), date_to=date(2025, 12, 31)
    )

    assert build_cache_key(
        "ranking_by_company", year_2024, limit=10
    ) != build_cache_key("ranking_by_company", year_2025, limit=10)
    assert build_cache_key(
        "ranking_by_company", year_2024, limit=10
    ) != build_cache_key("ranking_by_company", year_2024, limit=5)
    assert build_cache_key("kpi_summary", year_2024, limit=10) == build_cache_key(
        "kpi_summary", year_2024, limit=None
    )


def test_cache_miss_then_hit_calls_aggregate_once(monkeypatch) -> None:
    monkeypatch.setattr("app.services.query_cache.get_settings", lambda: _settings())
    aggregate = Mock(return_value=["fresh"])
    filters = AdjudicationFilters(article="laptop")

    first = cached_aggregate("monthly_trend", aggregate, Mock(), filters)
    second = cached_aggregate("monthly_trend", aggregate, Mock(), filters)

    assert first == ["fresh"]
    assert second == ["fresh"]
    aggregate.assert_called_once()


def test_cache_passes_limit_only_when_result_shape_uses_it(monkeypatch) -> None:
    monkeypatch.setattr("app.services.query_cache.get_settings", lambda: _settings())
    aggregate = Mock(return_value=["ranked"])
    filters = AdjudicationFilters()

    cached_aggregate("ranking_by_company", aggregate, Mock(), filters, limit=10)

    aggregate.assert_called_once_with(aggregate.call_args.args[0], filters, limit=10)


def test_cache_rejects_non_whitelisted_aggregates(monkeypatch) -> None:
    monkeypatch.setattr("app.services.query_cache.get_settings", lambda: _settings())
    aggregate = Mock(return_value="never")

    with pytest.raises(ValueError, match="not cacheable"):
        cached_aggregate("list_adjudications", aggregate, Mock(), AdjudicationFilters())

    aggregate.assert_not_called()


def test_cache_entry_expires_when_monotonic_clock_advances(monkeypatch) -> None:
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


def test_zero_ttl_always_bypasses_lookup_and_storage(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.query_cache.get_settings", lambda: _settings(ttl=0)
    )
    aggregate = Mock(side_effect=["first", "second"])
    filters = AdjudicationFilters()

    assert cached_aggregate("kpi_summary", aggregate, Mock(), filters) == "first"
    assert cached_aggregate("kpi_summary", aggregate, Mock(), filters) == "second"
    assert aggregate.call_count == 2


def test_cache_evicts_least_recently_used_entry(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.query_cache.get_settings",
        lambda: _settings(max_entries=2),
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


def test_clear_cache_forces_a_new_aggregate_execution(monkeypatch) -> None:
    monkeypatch.setattr("app.services.query_cache.get_settings", lambda: _settings())
    aggregate = Mock(side_effect=["before", "after"])
    filters = AdjudicationFilters()

    assert cached_aggregate("kpi_summary", aggregate, Mock(), filters) == "before"
    clear_cache()
    assert cached_aggregate("kpi_summary", aggregate, Mock(), filters) == "after"
    assert aggregate.call_count == 2
