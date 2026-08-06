"""Process-local TTL/LRU cache for whitelisted dashboard and company aggregates."""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING, Any, TypeVar, cast

from app.config import get_settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.services.adjudication_service import AdjudicationFilters

T = TypeVar("T")
AggregateFn = Callable[..., T]

_CACHEABLE_AGGREGATES = frozenset(
    {
        "kpi_summary",
        "monthly_trend",
        "concentration_ratio",
        "ranking_by_company",
        "ranking_by_organism",
        "distinct_organisms",
        "company_win_rate",
        "company_competitors",
        "company_summary",
        "top_articles",
    }
)
_LIMIT_AWARE_AGGREGATES = frozenset(
    {"ranking_by_company", "ranking_by_organism", "distinct_organisms", "top_articles"}
)
_CacheEntry = tuple[float, Any]
_cache: OrderedDict[str, _CacheEntry] = OrderedDict()
_cache_lock = threading.RLock()


def _normalize(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return tuple(_normalize(item) for item in value)
    return value


def build_cache_key(
    aggregate_name: str,
    filters: AdjudicationFilters,
    *,
    limit: int | None = None,
) -> str:
    """Build a stable, collision-safe key for one aggregate/filter shape."""

    values = (
        aggregate_name,
        _normalize(filters.company),
        _normalize(filters.company_doc_exact),
        _normalize(filters.organism),
        _normalize(filters.organism_exact),
        _normalize(filters.article),
        _normalize(filters.article_id),
        _normalize(filters.date_from),
        _normalize(filters.date_to),
        _normalize(limit) if aggregate_name in _LIMIT_AWARE_AGGREGATES else None,
    )
    return json.dumps(values, separators=(",", ":"), ensure_ascii=False)


def cached_aggregate(
    aggregate_name: str,
    aggregate: AggregateFn[T],
    session: Session,
    filters: AdjudicationFilters,
    *,
    limit: int | None = None,
) -> T:
    """Return a cached aggregate value or execute and store a cache miss."""

    if aggregate_name not in _CACHEABLE_AGGREGATES:
        raise ValueError(f"Aggregate {aggregate_name!r} is not cacheable")

    settings = get_settings()
    ttl = settings.cache_ttl_seconds
    if ttl == 0:
        return aggregate(session, filters)

    key = build_cache_key(aggregate_name, filters, limit=limit)
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None:
            created_at, value = entry
            if now - created_at < ttl:
                _cache.move_to_end(key)
                return cast("T", value)
            del _cache[key]

    if aggregate_name in _LIMIT_AWARE_AGGREGATES and limit is not None:
        value = aggregate(session, filters, limit=limit)
    else:
        value = aggregate(session, filters)

    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
        _cache.move_to_end(key)
        while len(_cache) > settings.cache_max_entries:
            _cache.popitem(last=False)
    return value


def clear_cache() -> None:
    """Remove every cached aggregate value."""

    with _cache_lock:
        _cache.clear()
