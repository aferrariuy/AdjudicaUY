"""Permanent compatibility facade for the split adjudication services.

New code should import from the domain-specific service modules directly.
This documented compatibility/deprecation layer remains available for legacy
consumers; removing it is a separate future change.
"""

from app.services.catalog import all_companies, all_organisms
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
    MAX_TOP_ARTICLES,
    ArticleRanking,
    ConcentrationResult,
    KpiSummary,
    RankingEntry,
    concentration_ratio,
    distinct_organisms,
    kpi_summary,
    monthly_trend,
    ranking_by_company,
    ranking_by_organism,
    top_articles,
)
from app.services.filters import (
    AdjudicationFilters,
    DateValidationError,
    ValidationError,
    filters_from_query_params,
    validate_date_params,
)
from app.services.listing import (
    MAX_EXPORT_ROWS,
    AdjudicationRow,
    count_adjudications,
    iter_adjudications,
    list_adjudications,
)

__all__ = [
    "AdjudicationFilters",
    "AdjudicationRow",
    "ArticleRanking",
    "CompanyProfileSummary",
    "CompanyWinRate",
    "ConcentrationResult",
    "DateValidationError",
    "KpiSummary",
    "MAX_EXPORT_ROWS",
    "MAX_TOP_ARTICLES",
    "RankingEntry",
    "ValidationError",
    "all_companies",
    "all_organisms",
    "company_competitors",
    "company_summary",
    "company_win_rate",
    "concentration_ratio",
    "count_adjudications",
    "distinct_organisms",
    "filters_from_query_params",
    "iter_adjudications",
    "kpi_summary",
    "list_adjudications",
    "lookup_company_identities",
    "lookup_company_identity",
    "monthly_trend",
    "ranking_by_company",
    "ranking_by_organism",
    "top_articles",
    "validate_date_params",
    "_document_pair_match",
    "_scoped_compra_ids",
]
