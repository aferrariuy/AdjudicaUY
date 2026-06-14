"""SQLAlchemy model for the ``adjudications`` table.

The table holds one row per scraped adjudication record, normalized to UYU at
ingestion time. Uniqueness is enforced on the triple ``(source_url,
license_link, date)`` so re-scraping the same source is idempotent while
collisions from different sources remain distinct (see data-storage spec,
"Duplicate Prevention" scenarios).
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)

from app.database import Base


class Adjudication(Base):
    __tablename__ = "adjudications"

    id = Column(Integer, primary_key=True)

    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(3), nullable=False)
    amount_uyu = Column(Numeric(12, 2), nullable=True)

    winning_company = Column(String(255), nullable=False)
    company_document = Column(String(50), nullable=True)
    company_document_type = Column(String(10), nullable=True)
    organism = Column(String(255), nullable=False)

    date = Column(Date, nullable=False, index=True)

    license_type = Column(String(100), nullable=True)
    article = Column(String(255), nullable=False, index=True)
    article_quantity = Column(Numeric(10, 2), nullable=True)
    article_id = Column(String(50), nullable=True, index=True)

    license_link = Column(String(512), nullable=True)
    source_url = Column(String(512), nullable=False)

    ingested_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_company", "winning_company"),
        Index("ix_organism", "organism"),
        UniqueConstraint("source_url", "license_link", "date", name="uq_source"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"<Adjudication id={self.id} date={self.date} "
            f"company={self.winning_company!r} amount={self.amount} {self.currency}>"
        )
