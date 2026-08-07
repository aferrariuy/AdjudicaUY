"""Company profile, identity, and competitor aggregate services."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from sqlalchemy import and_, func, or_, select

from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from app.models.oferente import Oferente
from app.services.filters import AdjudicationFilters, _apply_filters

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass(frozen=True)
class CompanyProfileSummary:
    """Company-profile KPIs for one exact provider-document identity."""

    display_name: str | None
    total_amount: Decimal
    total: int
    purchase_count: int
    organism_count: int
    share_of_total: Decimal


@dataclass(frozen=True)
class CompanyWinRate:
    participations: int
    wins: int
    rate: Decimal | None


@dataclass(frozen=True)
class CompanyCompetitor:
    company_type: str
    company_number: str
    display_name: str
    purchase_count: int
    awarded_amount_uyu: Decimal

    @property
    def company_profile_url(self) -> str:
        return (
            f"/company/{quote(self.company_type, safe='')}/"
            f"{quote(self.company_number, safe='')}"
        )


def lookup_company_identity(
    session: Session, company_type: str, company_number: str
) -> str | None:
    """Return the latest commercial name for an exact document pair."""

    stmt = (
        select(Adjudicacion.nombre_comercial)
        .join(Compra, Compra.id == Adjudicacion.compra_id)
        .where(
            Adjudicacion.tipo_doc_prov == company_type,
            Adjudicacion.nro_doc_prov == company_number,
        )
        .order_by(Compra.fecha_pub_adj.desc(), Adjudicacion.id.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def lookup_company_identities(
    session: Session, company_pairs: list[tuple[str, str]]
) -> dict[tuple[str, str], str | None]:
    if not company_pairs:
        return {}
    predicates = [
        and_(
            Adjudicacion.tipo_doc_prov == company_type,
            Adjudicacion.nro_doc_prov == company_number,
        )
        for company_type, company_number in set(company_pairs)
    ]
    stmt = (
        select(
            Adjudicacion.tipo_doc_prov,
            Adjudicacion.nro_doc_prov,
            Adjudicacion.nombre_comercial,
        )
        .join(Compra, Compra.id == Adjudicacion.compra_id)
        .where(or_(*predicates))
        .order_by(
            Adjudicacion.tipo_doc_prov,
            Adjudicacion.nro_doc_prov,
            Compra.fecha_pub_adj.desc(),
            Adjudicacion.id.desc(),
        )
    )
    identities: dict[tuple[str, str], str | None] = {}
    for row in session.execute(stmt):
        identities.setdefault((row[0], row[1]), row[2])
    return identities


def _without_company_identity(filters: AdjudicationFilters) -> AdjudicationFilters:
    return AdjudicationFilters(
        organism=filters.organism,
        organism_exact=filters.organism_exact,
        article=filters.article,
        article_id=filters.article_id,
        date_from=filters.date_from,
        date_to=filters.date_to,
    )


def _scoped_compra_ids(session: Session, filters: AdjudicationFilters) -> Any:
    stmt = select(Compra.id)
    scope_filters = _without_company_identity(filters)
    if scope_filters.article or scope_filters.article_id:
        stmt = stmt.join(Adjudicacion, Adjudicacion.compra_id == Compra.id)
    return _apply_filters(stmt, scope_filters)


def _document_pair_match(model: Any, company_type: str, company_number: str) -> Any:
    return and_(
        model.tipo_doc_prov.is_not(None),
        model.nro_doc_prov.is_not(None),
        model.tipo_doc_prov == company_type,
        model.nro_doc_prov == company_number,
    )


def company_win_rate(
    session: Session,
    company_type: str,
    company_number: str,
    filters: AdjudicationFilters,
) -> CompanyWinRate:
    """Return inclusive wins divided by distinct company participations."""

    scope = _scoped_compra_ids(session, filters).subquery("company_scope")
    target_bids = (
        select(Oferente.compra_id.label("compra_id"))
        .where(
            Oferente.compra_id.in_(select(scope.c.id)),
            _document_pair_match(Oferente, company_type, company_number),
        )
        .distinct()
        .cte("target_bids")
    )
    target_wins = (
        select(Adjudicacion.compra_id.label("compra_id"))
        .where(
            Adjudicacion.compra_id.in_(select(scope.c.id)),
            _document_pair_match(Adjudicacion, company_type, company_number),
        )
        .distinct()
        .cte("target_wins")
    )
    participations_stmt = select(func.count()).select_from(
        select(target_bids.c.compra_id)
        .union(select(target_wins.c.compra_id))
        .subquery()
    )
    stmt = select(
        participations_stmt.scalar_subquery().label("participations"),
        select(func.count()).select_from(target_wins).scalar_subquery().label("wins"),
    )
    row = session.execute(stmt).one()
    participations = int(row.participations or 0)
    wins = int(row.wins or 0)
    return CompanyWinRate(
        participations=participations,
        wins=wins,
        rate=Decimal(wins) / Decimal(participations) if participations else None,
    )


def company_competitors(
    session: Session,
    company_type: str,
    company_number: str,
    filters: AdjudicationFilters,
    *,
    limit: int = 5,
) -> list[CompanyCompetitor]:
    """Return deterministic co-bidder rankings for the target company."""

    scoped = _scoped_compra_ids(session, filters).subquery("company_scope")
    target = (
        select(Oferente.compra_id.label("compra_id"))
        .where(
            Oferente.compra_id.in_(select(scoped.c.id)),
            _document_pair_match(Oferente, company_type, company_number),
        )
        .distinct()
        .cte("target")
    )
    valid_competitor = and_(
        Oferente.tipo_doc_prov.is_not(None),
        Oferente.nro_doc_prov.is_not(None),
        ~and_(
            Oferente.tipo_doc_prov == company_type,
            Oferente.nro_doc_prov == company_number,
        ),
    )
    bids = (
        select(
            Oferente.compra_id.label("compra_id"),
            Oferente.tipo_doc_prov.label("company_type"),
            Oferente.nro_doc_prov.label("company_number"),
            func.max(Oferente.nombre_comercial).label("fallback_name"),
        )
        .where(Oferente.compra_id.in_(select(target.c.compra_id)), valid_competitor)
        .group_by(
            Oferente.compra_id,
            Oferente.tipo_doc_prov,
            Oferente.nro_doc_prov,
        )
        .cte("bids")
    )
    awards = (
        select(
            Adjudicacion.compra_id.label("compra_id"),
            Adjudicacion.tipo_doc_prov.label("company_type"),
            Adjudicacion.nro_doc_prov.label("company_number"),
            func.sum(Adjudicacion.amount_uyu).label("awarded_amount_uyu"),
        )
        .where(
            Adjudicacion.compra_id.in_(select(target.c.compra_id)),
            Adjudicacion.tipo_doc_prov.is_not(None),
            Adjudicacion.nro_doc_prov.is_not(None),
        )
        .group_by(
            Adjudicacion.compra_id,
            Adjudicacion.tipo_doc_prov,
            Adjudicacion.nro_doc_prov,
        )
        .cte("awards")
    )
    stmt = (
        select(
            bids.c.company_type,
            bids.c.company_number,
            func.max(bids.c.fallback_name).label("fallback_name"),
            func.count(func.distinct(bids.c.compra_id)).label("purchase_count"),
            func.coalesce(func.sum(awards.c.awarded_amount_uyu), 0).label(
                "awarded_amount_uyu"
            ),
        )
        .select_from(bids)
        .outerjoin(
            awards,
            and_(
                awards.c.compra_id == bids.c.compra_id,
                awards.c.company_type == bids.c.company_type,
                awards.c.company_number == bids.c.company_number,
            ),
        )
        .group_by(bids.c.company_type, bids.c.company_number)
    )
    rows = list(session.execute(stmt))
    pairs = [(row.company_type, row.company_number) for row in rows]
    identities = lookup_company_identities(session, pairs)
    competitors = [
        CompanyCompetitor(
            company_type=row.company_type,
            company_number=row.company_number,
            display_name=identities.get((row.company_type, row.company_number))
            or row.fallback_name
            or "Empresa sin nombre",
            purchase_count=int(row.purchase_count),
            awarded_amount_uyu=Decimal(row.awarded_amount_uyu or 0),
        )
        for row in rows
    ]
    competitors.sort(
        key=lambda row: (
            -row.purchase_count,
            -row.awarded_amount_uyu,
            row.display_name,
        )
    )
    return competitors[: max(limit, 0)]


def company_summary(
    session: Session,
    filters: AdjudicationFilters,
    *,
    market_total: Decimal | None = None,
) -> CompanyProfileSummary:
    """Return exact-document KPIs and share of the filtered market total."""

    total_expr = func.coalesce(func.sum(Adjudicacion.amount_uyu), 0).label(
        "total_amount"
    )
    count_expr = func.count(Adjudicacion.id).label("total")
    purchase_expr = func.count(func.distinct(Compra.id)).label("purchase_count")
    organism_expr = func.count(func.distinct(Compra.organismo)).label("organism_count")
    company_stmt = select(total_expr, count_expr, purchase_expr, organism_expr).join(
        Compra, Compra.id == Adjudicacion.compra_id
    )
    company_stmt = _apply_filters(company_stmt, filters)
    company_row = session.execute(company_stmt).one()
    total = Decimal(company_row.total_amount or 0)

    if market_total is None:
        market_filters = AdjudicationFilters(
            company=filters.company,
            organism=filters.organism,
            organism_exact=filters.organism_exact,
            article=filters.article,
            article_id=filters.article_id,
            date_from=filters.date_from,
            date_to=filters.date_to,
        )
        market_stmt = select(func.coalesce(func.sum(Adjudicacion.amount_uyu), 0)).join(
            Compra, Compra.id == Adjudicacion.compra_id
        )
        market_stmt = _apply_filters(market_stmt, market_filters)
        market_total = Decimal(session.execute(market_stmt).scalar_one() or 0)
    share = total / market_total if market_total > 0 else Decimal(0)

    return CompanyProfileSummary(
        display_name=None,
        total_amount=total,
        total=int(company_row.total or 0),
        purchase_count=int(company_row.purchase_count or 0),
        organism_count=int(company_row.organism_count or 0),
        share_of_total=share,
    )
